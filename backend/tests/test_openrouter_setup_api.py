"""Deterministic operator setup tests for the fixed OpenRouter settings route."""

from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import settings
from src.llm_runtime import _profile_api_key, build_completion_kwargs, provider_profiles
from src.model_fabric.configuration import (
    OPENROUTER_VAULT_CREDENTIAL_REF,
    hydrate_openrouter_credential,
)
from src.model_fabric.remote_inference_admission import remote_inference_admission_broker
from src.vault.repository import vault_repository


@pytest.fixture
def model_fabric_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path.parent / f"{tmp_path.name}-lifecycle"))
    from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
    prepare_lifecycle_directory(ProductionWorkspace(host_root=tmp_path))
    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(settings, "openrouter_allow_fallbacks", False)
    monkeypatch.setattr(settings, "openrouter_require_parameters", True)
    monkeypatch.setattr(settings, "openrouter_data_collection", "deny")
    monkeypatch.setattr(settings, "openrouter_zero_data_retention", True)
    monkeypatch.setattr(settings, "screen_analysis_model", "")
    yield tmp_path


def _setup_payload(**overrides):
    payload = {
        "model_ids": ["anthropic/claude-sonnet-4"],
        "capabilities": ["text", "structured_output"],
        "temperature": 0.4,
        "max_output_tokens": 2048,
        "timeout_seconds": 45,
        "allowed_upstreams": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "data_retention_policy": "deny",
        "zero_data_retention": False,
        "egress_class": "cloud_allowed_full",
        "cloud_egress_acknowledged": True,
        "spend_ceiling_microusd": 25_000,
        "max_queued": 8,
        "max_inflight": 1,
        "max_outstanding_per_owner": 4,
        "max_retries": 1,
    }
    payload.update(overrides)
    return payload


def _v2_setup_payload(**overrides):
    # Synthetic identities are used only behind intercepted provider boundaries.
    def route(slot):
        return {"model_id": f"fixture/{slot}", "enabled": True,
            "capabilities": {"text": ["text"], "vision": ["text", "vision", "structured_output"], "embedding": ["embedding"]}[slot],
            "allowed_upstreams": ["fixture"], "temperature": 0.4,
            "max_output_tokens": 2048, "timeout_seconds": 45,
            "zero_data_retention": slot != "text", "request_cost_bound_microusd": 100}
    payload = {"schema_version": "seraph.openrouter.setup.v2",
        "routes": {slot: route(slot) for slot in ("text", "vision", "embedding")},
        "data_collection": "deny", "data_retention_policy": "deny",
        "egress_class": "cloud_allowed_full", "cloud_egress_acknowledged": True,
        "vision_egress_acknowledged": True, "embedding_egress_acknowledged": True,
        "spend_ceiling_microusd": 25_000, "max_queued": 8, "max_inflight": 1,
        "max_outstanding_per_owner": 4, "max_retries": 1}
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_slots_save_readback_restart_and_legacy_write_rejection(client, model_fabric_workspace, keyless_openrouter):
    from src.model_fabric.configuration import read_model_fabric_configuration, effective_workload_policy
    from src.llm_runtime import resolve_runtime_profile
    with patch("httpx.AsyncClient.post", side_effect=AssertionError("provider contact forbidden")):
        response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload()})
        assert response.status_code == 200, response.text
        assert response.json()["egress_revision"] == 3
        saved = read_model_fabric_configuration()
        assert saved.egress_revoked is False
        assert saved.openrouter_setup.purpose_consents == {"vision": 3, "embedding": 3}
        assert set(profile.id for profile in saved.profiles) == {"openrouter.text", "openrouter.vision", "openrouter.embedding"}
        for runtime, slot in (("chat_agent", "text"), ("screenshot_image_analysis", "vision"), ("memory_embedding", "embedding")):
            assert resolve_runtime_profile(runtime_path=runtime) == f"openrouter.{slot}"
            assert effective_workload_policy(runtime).allowed_profile_ids == (f"openrouter.{slot}",)
        # Independent reopening/hydration has no authority to change the file.
        before = (model_fabric_workspace / "model-fabric-settings.json").read_bytes()
        settings.openrouter_api_key = ""
        restarted = await client.get("/api/settings/model-fabric")
        assert restarted.status_code == 200
        assert restarted.json()["openrouter_setup"]["schema_version"] == "seraph.openrouter.setup.v2"
        assert all(state["status"] == "configuration_required" for state in restarted.json()["openrouter_setup"]["slot_statuses"].values())
        assert (model_fabric_workspace / "model-fabric-settings.json").read_bytes() == before
        legacy = await client.put("/api/settings/model-fabric", json={"openrouter": _setup_payload()})
        assert legacy.status_code == 409
        assert legacy.json()["detail"] == "setup_schema_upgrade_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_save_race_has_exactly_one_success(client, model_fabric_workspace, keyless_openrouter):
    import asyncio
    body = {"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload()}
    replies = await asyncio.gather(*(client.put("/api/settings/model-fabric", json=body) for _ in range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    status = (await client.get("/api/settings/model-fabric")).json()
    assert status["egress_revision"] == 3
    assert status["inference_accounting"]["ceiling_microusd"] == 25_000


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_requires_explicit_purpose_consent_before_credential_mutation(client, model_fabric_workspace, keyless_openrouter):
    body = _v2_setup_payload(vision_egress_acknowledged=False, api_key="sk-forbidden-test")
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": body})
    assert response.status_code == 403
    assert await vault_repository.get("openrouter_api_key") is None
    assert not (model_fabric_workspace / "model-fabric-settings.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_interrupted_accounting_leaves_revoked_and_compensates_only_key(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.workflows.job_runtime import durable_job_repository
    from src.model_fabric.configuration import read_model_fabric_configuration
    async def broken_accounting(*args, **kwargs):
        assert read_model_fabric_configuration().egress_revoked is True
        raise RuntimeError("injected accounting failure")
    monkeypatch.setattr(durable_job_repository, "configure_inference_accounting", broken_accounting)
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-new-fixture")})
    assert response.status_code == 503
    saved = read_model_fabric_configuration()
    assert saved.egress_revision == 2
    assert saved.egress_revoked is True
    assert saved.openrouter_setup.schema_version == "seraph.openrouter.setup.v2"
    assert settings.openrouter_api_key == ""
    assert await vault_repository.get("openrouter_api_key") is None
    assert (await client.get("/api/settings/model-fabric")).json()["egress_revoked"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_uncertain_final_publication_preserves_active_target_key(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.api import model_fabric_settings as api
    from src.model_fabric.configuration import read_model_fabric_configuration
    write = api.write_model_fabric_configuration
    def write_then_fail(configuration, **kwargs):
        write(configuration, **kwargs)
        if not configuration.egress_revoked:
            raise OSError("uncertain active publication")
    monkeypatch.setattr(api, "write_model_fabric_configuration", write_then_fail)
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-active-fixture")})
    assert response.status_code == 503
    assert read_model_fabric_configuration().egress_revision == 3
    assert read_model_fabric_configuration().egress_revoked is False
    assert settings.openrouter_api_key == "sk-active-fixture"
    assert await vault_repository.get("openrouter_api_key") == "sk-active-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_lost_initial_cas_never_mutates_or_compensates_staged_key(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from unittest.mock import AsyncMock
    from src.api import model_fabric_settings as api
    from src.workspace.accounting_witness import PolicyRevisionConflict
    from src.model_fabric.configuration import read_model_fabric_configuration
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-prior-fixture")})
    assert saved.status_code == 200
    before = (model_fabric_workspace / "model-fabric-settings.json").read_bytes()
    store = AsyncMock(side_effect=AssertionError("CAS loser must not install staged key"))
    restore = AsyncMock(side_effect=AssertionError("unmutated key must not compensate"))
    def lose_cas(*args, **kwargs):
        raise PolicyRevisionConflict("provider_policy_revision_changed")
    monkeypatch.setattr(api, "write_model_fabric_configuration", lose_cas)
    monkeypatch.setattr(api, "_store_setup_credential", store)
    monkeypatch.setattr(api, "_restore_setup_credential", restore)
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3, "openrouter_setup": _v2_setup_payload(api_key="sk-losing-fixture")})
    assert response.status_code == 409
    store.assert_not_awaited()
    restore.assert_not_awaited()
    assert (model_fabric_workspace / "model-fabric-settings.json").read_bytes() == before
    assert read_model_fabric_configuration().egress_revision == 3
    assert settings.openrouter_api_key == "sk-prior-fixture"
    assert await vault_repository.get("openrouter_api_key") == "sk-prior-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_key_commit_then_raise_compensates_with_revoked_readback(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.api import model_fabric_settings as api
    from src.model_fabric.configuration import read_model_fabric_configuration
    store = api._store_setup_credential
    async def commit_then_fail(*args, **kwargs):
        await store(*args, **kwargs)
        raise RuntimeError("injected key writer uncertainty")
    monkeypatch.setattr(api, "_store_setup_credential", commit_then_fail)
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-uncertain-key-fixture")})
    assert response.status_code == 503
    assert read_model_fabric_configuration().egress_revoked is True
    assert read_model_fabric_configuration().egress_revision == 2
    assert settings.openrouter_api_key == ""
    assert await vault_repository.get("openrouter_api_key") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("mutation", (
    "legacy_model", "legacy_capabilities", "unknown_slot", "string_enabled",
    "boolean_bound", "missing_upstream", "mixed_embedding", "vision_without_text",
    "wrong_endpoint", "unsafe_key",
))
async def test_v2_closed_input_rejects_before_secret_or_policy_mutation(client, model_fabric_workspace, keyless_openrouter, mutation):
    body = _v2_setup_payload(api_key="sk-rejected-fixture")
    if mutation == "legacy_model":
        body["model"] = "fixture/ignored"
    elif mutation == "legacy_capabilities":
        body["capabilities"] = ["text"]
    elif mutation == "unknown_slot":
        body["routes"]["audio"] = None
    elif mutation == "string_enabled":
        body["routes"]["text"]["enabled"] = "true"
    elif mutation == "boolean_bound":
        body["routes"]["text"]["request_cost_bound_microusd"] = True
    elif mutation == "missing_upstream":
        body["routes"]["text"]["allowed_upstreams"] = []
    elif mutation == "mixed_embedding":
        body["routes"]["embedding"]["capabilities"] = ["embedding", "text"]
    elif mutation == "vision_without_text":
        body["routes"]["vision"]["capabilities"] = ["vision"]
    elif mutation == "wrong_endpoint":
        body["api_base"] = "https://provider.invalid/v1"
    elif mutation == "unsafe_key":
        body["api_key"] = "sk-unsafe\nfixture"
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": body})
    assert response.status_code == 422, response.text
    assert await vault_repository.get("openrouter_api_key") is None
    assert not (model_fabric_workspace / "model-fabric-settings.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_all_null_slots_save_without_inventing_selection(client, model_fabric_workspace, keyless_openrouter):
    from src.model_fabric.configuration import effective_workload_policy, read_model_fabric_configuration
    from src.security.trust_contract import EgressClass
    body = _v2_setup_payload(routes={"text": None, "vision": None, "embedding": None})
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": body})
    assert response.status_code == 200, response.text
    assert read_model_fabric_configuration().profiles == ()
    assert all(route is None for route in response.json()["openrouter_setup"]["routes"].values())
    assert all(state["status"] == "configuration_required" for state in response.json()["openrouter_setup"]["slot_statuses"].values())
    assert effective_workload_policy("chat_agent").egress_class is EgressClass.LOCAL_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("capabilities, expected_slots", (
    (["text", "structured_output"], {"text"}),
    (["text", "vision", "structured_output"], {"text", "vision"}),
    (["embedding"], {"embedding"}),
))
async def test_v1_projection_is_pure_and_explicit_v2_save_preserves_existing_consent(client, model_fabric_workspace, keyless_openrouter, capabilities, expected_slots):
    from dataclasses import asdict
    from src.model_fabric.configuration import migrate_openrouter_setup_v1_to_v2, read_model_fabric_configuration
    saved = await client.put("/api/settings/model-fabric", json={"openrouter_setup": _setup_payload(capabilities=capabilities, zero_data_retention=True, api_key="sk-migration-fixture")})
    assert saved.status_code == 200, saved.text
    original = read_model_fabric_configuration()
    before = (model_fabric_workspace / "model-fabric-settings.json").read_bytes()
    with patch("httpx.AsyncClient.post", side_effect=AssertionError("migration provider contact forbidden")):
        projected = (await client.get("/api/settings/model-fabric")).json()["openrouter_setup"]
    assert projected["schema_version"] == "seraph.openrouter.setup.v2"
    assert {slot for slot, route in projected["routes"].items() if route is not None} == expected_slots
    assert (model_fabric_workspace / "model-fabric-settings.json").read_bytes() == before
    assert read_model_fabric_configuration().openrouter_setup.schema_version == "seraph.openrouter.setup.v1"
    migration = migrate_openrouter_setup_v1_to_v2(original.openrouter_setup, egress_revision=original.egress_revision)
    assert migration.purpose_consents == {slot: original.egress_revision for slot in expected_slots - {"text"}}
    body = _v2_setup_payload(routes={slot: asdict(route) if route is not None else None for slot, route in migration.routes.items()},
        vision_egress_acknowledged=False, embedding_egress_acknowledged=False)
    if "vision" in capabilities:
        assert projected["slot_statuses"]["text"]["error_code"] == "legacy_route_capabilities_require_review"
        assert body["routes"]["text"]["capabilities"] == tuple(capabilities)
        # The explicit reviewed save owns choosing the valid text slot.
        body["routes"]["text"]["capabilities"] = [item for item in capabilities if item != "vision"]
    # Keep original values exactly; migration cannot silently raise cost/limits.
    body["spend_ceiling_microusd"] = original.openrouter_setup.spend_ceiling_microusd
    body["max_retries"] = original.openrouter_setup.max_retries
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": original.egress_revision, "openrouter_setup": body})
    assert response.status_code == 200, response.text
    current = read_model_fabric_configuration()
    assert current.v1_rollback_snapshot["openrouter_setup"]["schema_version"] == "seraph.openrouter.setup.v1"
    assert "sk-migration-fixture" not in (model_fabric_workspace / "model-fabric-settings.json").read_text()
    assert await vault_repository.get("openrouter_api_key") == "sk-migration-fixture"
    assert current.openrouter_setup.purpose_consents == {slot: current.egress_revision for slot in expected_slots - {"text"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("capabilities", (["vision"], ["structured_output"]))
async def test_v1_incompatible_projection_keeps_exact_capabilities_blocked_without_write(client, model_fabric_workspace, keyless_openrouter, capabilities):
    from src.model_fabric.configuration import migrate_openrouter_setup_v1_to_v2, read_model_fabric_configuration
    response = await client.put("/api/settings/model-fabric", json={"openrouter_setup": _setup_payload(capabilities=capabilities, zero_data_retention=True)})
    assert response.status_code == 200, response.text
    original = read_model_fabric_configuration()
    before = (model_fabric_workspace / "model-fabric-settings.json").read_bytes()
    migration = migrate_openrouter_setup_v1_to_v2(original.openrouter_setup, egress_revision=original.egress_revision)
    assert migration.routes["text"].capabilities == tuple(capabilities)
    status = (await client.get("/api/settings/model-fabric")).json()["openrouter_setup"]
    for slot, route in status["routes"].items():
        if route is not None:
            assert route["capabilities"] == capabilities
            assert route["status"] == "blocked"
            assert route["error_code"] == "legacy_route_capabilities_require_review"
    assert (model_fabric_workspace / "model-fabric-settings.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_saved_routes_own_runtime_and_screen_status_without_legacy_fallback(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.observer.screen_analysis_settings import read_screen_analysis_settings, write_screen_analysis_settings
    monkeypatch.setattr(settings, "screen_analysis_provider", "legacy")
    monkeypatch.setattr(settings, "screen_analysis_model", "openrouter/fixture/obsolete")
    write_screen_analysis_settings({"enabled": True, "provider": "legacy", "model": "openrouter/fixture/obsolete"})
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload()})
    assert saved.status_code == 200
    runtime = (await client.get("/api/runtime/status")).json()
    assert runtime["effective_runtime"]["model"] == "fixture/text"
    assert "openrouter_upstream_allowlist_missing" not in runtime["effective_runtime"]["inference_readiness"]["reasons"]
    screen = read_screen_analysis_settings()
    assert screen["enabled"] is True
    assert screen["model"] == "openrouter/fixture/vision"
    assert screen["provider"] == "openrouter"
    body = _v2_setup_payload()
    body["routes"]["vision"] = None
    assert (await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3, "openrouter_setup": body})).status_code == 200
    screen = read_screen_analysis_settings()
    assert screen["enabled"] is True
    assert screen["model"] == "" and screen["provider"] == ""
    assert (await client.get("/api/runtime/status")).json()["effective_runtime"]["model"] == "fixture/text"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_interrupted_active_witness_requires_revoked_reconciliation(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.workspace import accounting_witness, maintenance_fence
    from src.workspace.production import ProductionWorkspace
    from src.model_fabric.configuration import read_model_fabric_configuration
    writer = accounting_witness._write_configuration_file
    def fail_active_file(path, payload):
        if not payload["egress_revoked"]:
            raise OSError("injected witness-to-file crash")
        return writer(path, payload)
    monkeypatch.setattr(accounting_witness, "_write_configuration_file", fail_active_file)
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-gap-fixture")})
    assert response.status_code == 503
    status = (await client.get("/api/settings/model-fabric")).json()
    assert status["egress_revoked"] is True
    assert status["error_code"] == "provider_policy_continuity_unavailable"
    # Contradictory active witness cannot justify guessing a key compensation.
    assert await vault_repository.get("openrouter_api_key") == "sk-gap-fixture"
    monkeypatch.setattr(accounting_witness, "_write_configuration_file", writer)
    with maintenance_fence(ProductionWorkspace(host_root=model_fabric_workspace)):
        recovery = accounting_witness.reconcile_policy_checkpoint(model_fabric_workspace)
    assert recovery["status"] == "reconciled_revoked"
    assert recovery["revision"] == 4
    settings.openrouter_api_key = ""
    restarted = await client.get("/api/settings/model-fabric")
    assert restarted.json()["egress_revoked"] is True
    assert read_model_fabric_configuration().egress_revision == 4
    assert all(state["status"] != "ready" for state in restarted.json()["openrouter_setup"]["slot_statuses"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_cross_process_writer_cannot_activate_during_key_mutation_or_restore_old_policy(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    import asyncio
    import json
    import sys
    from src.api import model_fabric_settings as api
    from src.model_fabric.configuration import read_model_fabric_configuration
    store = api._store_setup_credential
    observations = []
    # This subprocess runs the actual retained flock and exact-CAS publication
    # seam against the same file, with no database/provider or credential access.
    script = """
import json, sys
from dataclasses import replace
from config.settings import settings
from src.model_fabric.configuration import read_model_fabric_configuration, write_model_fabric_configuration
settings.workspace_dir = sys.argv[1]
current = read_model_fabric_configuration()
try:
    write_model_fabric_configuration(replace(current, egress_revoked=False, egress_revision=3), expected_revision=2)
except Exception as error:
    print(json.dumps({"error": type(error).__name__, "reason": str(error), "revision": current.egress_revision, "revoked": current.egress_revoked}))
else:
    print(json.dumps({"unexpected_success": True}))
"""
    async def contender():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(model_fabric_workspace),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, error.decode()
        return json.loads(output)
    async def checked_store(*args, **kwargs):
        assert read_model_fabric_configuration().egress_revoked
        result = await store(*args, **kwargs)
        observations.append(await contender())
        assert read_model_fabric_configuration().egress_revision == 2
        assert read_model_fabric_configuration().egress_revoked
        return result
    monkeypatch.setattr(api, "_store_setup_credential", checked_store)
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-cross-process-fixture")})
    assert response.status_code == 200, response.text
    assert observations[0]["error"] == "ProductionWorkspaceReconciliationError"
    assert "busy" in observations[0]["reason"]
    assert observations[0]["revision"] == 2 and observations[0]["revoked"] is True
    after = await contender()
    assert after["error"] == "PolicyRevisionConflict"
    assert read_model_fabric_configuration().egress_revision == 3
    assert read_model_fabric_configuration().egress_revoked is False
    assert await vault_repository.get("openrouter_api_key") == "sk-cross-process-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_vision_edit_preserves_exact_text_proof_but_denies_old_queued_epoch(client, model_fabric_workspace, keyless_openrouter):
    import asyncio
    import time
    from dataclasses import replace
    from src.model_fabric import candidate_from_profile
    from src.model_fabric.accounting import bind_accounting_profile
    from src.model_fabric.configuration import read_model_fabric_configuration
    from src.model_fabric.proofs import build_model_route_proof, proof_is_fresh
    from src.model_fabric.repository import model_fabric_repository
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.workflows.job_runtime import durable_job_repository
    from tests.test_inference_accounting import request
    from tests.test_model_fabric_proofs_receipts import _receipt

    first_save = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-proof-fixture")})
    assert first_save.status_code == 200
    before = next(profile for profile in read_model_fabric_configuration().profiles if profile.id == "openrouter.text")
    candidate = candidate_from_profile(before)
    # Synthetic proof fixture checks exact hash/storage semantics only. It does
    # not substitute for the milestone's intercepted manual canary journey.
    now = time.time()
    receipt = _receipt(receipt_id="fixture-text-probe", workload="capability_probe")
    receipt = replace(receipt, actual_profile_id=before.id, actual_model=before.model,
        actual_adapter=before.transport_adapter,
        attempts=tuple(replace(attempt, profile_id=before.id, model=before.model,
            endpoint=candidate.endpoint, adapter=before.transport_adapter) for attempt in receipt.attempts))
    assert (await model_fabric_repository.persist_route_receipt(receipt)).persisted
    proof = build_model_route_proof(profile=before, endpoint_class=candidate.endpoint_class,
        adapter=before.transport_adapter, capability="text", canary_version="fixture-v1",
        outcome="passed", checked_at=now, expires_at=now + 600,
        probe_receipt_id=receipt.receipt_id, probe_receipt_hash=receipt.receipt_hash)
    assert (await model_fabric_repository.persist_capability_proof(proof)).persisted
    for capability, value in (("health", "healthy"), ("latency_ms", 1)):
        additional = build_model_route_proof(profile=before, endpoint_class=candidate.endpoint_class,
            adapter=before.transport_adapter, capability=capability, canary_version="fixture-v1",
            outcome="passed", checked_at=now, expires_at=now + 600,
            probe_receipt_id=receipt.receipt_id, probe_receipt_hash=receipt.receipt_hash, proven_value=value)
        assert (await model_fabric_repository.persist_capability_proof(additional)).persisted
    from datetime import datetime, timezone
    ready = (await client.get("/api/settings/model-fabric")).json()["openrouter_setup"]["slot_statuses"]["text"]
    assert ready["status"] == "ready"
    assert ready["proof_expires_at"] == datetime.fromtimestamp(now + 600, timezone.utc).isoformat()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    started, release = asyncio.Event(), asyncio.Event()
    callbacks = []
    async def active():
        callbacks.append("active")
        started.set()
        await release.wait()
        return {"usage": {"cost": "0.000001"}}
    async def forbidden():
        callbacks.append("stale-queued")
        return {"usage": {"cost": "0.000001"}}
    bind_accounting_profile("active-text", before.id)
    bind_accounting_profile("queued-text", before.id)
    active_task = asyncio.create_task(broker.execute(replace(request("active-text"), owner_budget_microusd=25_000), active))
    queued_task = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        queued_task = asyncio.create_task(broker.execute(replace(request("queued-text"), owner_budget_microusd=25_000), forbidden))
        for _ in range(100):
            operations = (await durable_job_repository.inference_accounting_snapshot()).get("operations", [])
            if len(operations) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(operations) == 2
        changed = _v2_setup_payload()
        changed["routes"]["vision"]["model_id"] = "fixture/new-vision"
        second_save = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3, "openrouter_setup": changed})
        assert second_save.status_code == 200, second_save.text
        after = next(profile for profile in read_model_fabric_configuration().profiles if profile.id == before.id)
        assert after.contract_hash == before.contract_hash
        found = await model_fabric_repository.latest_capability_proof(profile_schema_version=after.schema_version,
            profile_contract_hash=after.contract_hash, profile_id=after.id, model=after.model,
            endpoint=candidate.endpoint, endpoint_class=candidate.endpoint_class,
            adapter=after.transport_adapter, capability="text")
        assert found is not None and found.proof_hash == proof.proof_hash and proof_is_fresh(found)
    finally:
        release.set()
        tasks = [task for task in (active_task, queued_task) if task is not None]
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    assert all(isinstance(outcome, Exception) for outcome in outcomes)
    assert callbacks == ["active"]
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    assert snapshot["committed_microusd"] == 1
    assert snapshot["unknown_microusd"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v2_native_manual_canaries_use_exact_slots_shared_accounting_and_measured_embedding_dimension(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    import httpx
    from src.api import model_fabric_settings as api
    from src.workflows.job_runtime import durable_job_repository
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "fixture-auth-password")
    client.headers["origin"] = "http://localhost:3001"
    login = await client.post("/api/auth/login", json={"password": "fixture-auth-password"})
    assert login.status_code == 200, login.text
    calls = []
    import threading
    contact_lock = threading.Lock()
    contact_state = {"active": 0, "peak": 0}
    ordinary_order = []
    hold_embedding = [False]
    embedding_started, release_embedding = threading.Event(), threading.Event()
    failures = []
    execute = api.remote_inference_admission_broker.execute
    async def checked_execute(*args, **kwargs):
        try:
            return await execute(*args, **kwargs)
        except Exception as error:
            failures.append((type(error).__name__, str(error), type(error.__cause__).__name__, str(error.__cause__)))
            raise
    monkeypatch.setattr(api.remote_inference_admission_broker, "execute", checked_execute)
    class ProviderClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def post(self, endpoint, *, json, headers):
            calls.append((endpoint, json, headers))
            with contact_lock:
                contact_state["active"] += 1
                contact_state["peak"] = max(contact_state["peak"], contact_state["active"])
            if hold_embedding[0]:
                ordinary_order.append(json["model"])
            assert endpoint.startswith("https://openrouter.ai/api/v1/")
            assert json["provider"]["only"] == ["fixture"]
            assert json["provider"]["allow_fallbacks"] is False
            result = {"id": "fixture-operation", "usage": {"cost": "0.000001"}}
            if endpoint.endswith("/embeddings"):
                assert "input" in json and "messages" not in json
                result["data"] = [{"index": 0, "embedding": [0.3, 0.4, 0.5]}]
            else:
                assert "messages" in json and "input" not in json
                if json["model"] == "fixture/vision":
                    from tests.test_openrouter_screenshot import _analysis_payload
                    import json as codec
                    content = codec.dumps(_analysis_payload())
                else:
                    content = '{"ok":true}'
                result["choices"] = [{"message": {"content": content}}]
            with contact_lock:
                contact_state["active"] -= 1
            return httpx.Response(200, json=result, request=httpx.Request("POST", endpoint))
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": _v2_setup_payload(api_key="sk-canary-fixture")})
    assert saved.status_code == 200
    monkeypatch.setattr(api.httpx, "AsyncClient", ProviderClient)
    expected_calls = 0
    for slot, capabilities in (("text", ("text",)), ("vision", ("text", "vision", "structured_output")), ("embedding", ("embedding",))):
        for capability in (*capabilities, "health", "latency_ms"):
            response = await client.post("/api/settings/model-fabric/canary", json={"profile_id": f"openrouter.{slot}",
                "capability": capability, "timeout_seconds": 45, "proof_ttl_seconds": 600})
            assert response.status_code == 200, response.text
            assert response.json()["outcome"] == "passed", (response.text, failures)
            proof = response.json()["proof"]
            assert proof["profile_id"] == f"openrouter.{slot}"
            if capability == "embedding":
                assert proof["proven_value"] == 3
            expected_calls += 1
    assert len(calls) == expected_calls == 11
    assert {body["model"] for _, body, _ in calls} == {"fixture/text", "fixture/vision", "fixture/embedding"}
    status = (await client.get("/api/settings/model-fabric")).json()
    assert all(state["status"] == "ready" for state in status["openrouter_setup"]["slot_statuses"].values())
    ledger = await durable_job_repository.inference_accounting_snapshot()
    assert ledger["committed_microusd"] == 11 and ledger["unknown_microusd"] == 0
    assert {operation["profile_id"] for operation in ledger["operations"]} == {"openrouter.text", "openrouter.vision", "openrouter.embedding"}
    assert "sk-canary-fixture" not in status.__str__()

    import asyncio
    from src.approval.runtime import set_runtime_context, reset_runtime_context
    from src.auth.service import authenticate_session
    from src.memory import embedder, vector_store
    operator = await authenticate_session(login.json()["session_id"], touch=False)
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    class EmbeddingClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["timeout"].read <= 45
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def post(self, endpoint, *, json, headers):
            calls.append((endpoint, json, headers))
            with contact_lock:
                contact_state["active"] += 1
                contact_state["peak"] = max(contact_state["peak"], contact_state["active"])
            if hold_embedding[0]:
                ordinary_order.append(json["model"])
                if endpoint.endswith("/embeddings"):
                    embedding_started.set()
                    assert release_embedding.wait(10), "bounded native embedding did not release"
            result = {"id": "fixture-native", "usage": {"cost": "0.000001"}}
            if endpoint.endswith("/embeddings"):
                assert json["model"] == "fixture/embedding"
                result["data"] = [{"index": 0, "embedding": [0.3, 0.4, 0.5]}]
            else:
                assert endpoint.endswith("/chat/completions") and json["model"] == "fixture/text"
                result["choices"] = [{"message": {"content": "native chat fixture readback"}}]
            response = httpx.Response(200, json=result, request=httpx.Request("POST", endpoint))
            with contact_lock:
                contact_state["active"] -= 1
            return response
    monkeypatch.setattr(embedder.httpx, "Client", EmbeddingClient)
    monkeypatch.setattr(vector_store, "_db", None)
    embedder._reset_embedder_state()
    try:
        metadata = await asyncio.to_thread(embedder.embedding_metadata)
        assert metadata.model == "fixture/embedding" and metadata.dimension == 3
        assert await asyncio.to_thread(vector_store.search_with_status, "no index") == ([], True)
        assert len(calls) == 11
        assert vector_store._get_db().table_names() == []
        memory_id = await asyncio.to_thread(vector_store.add_memory, "explicit existing indexing fixture")
        assert memory_id
        table_name = vector_store._table_name(metadata)
        assert vector_store._get_db().table_names() == [table_name]
        # Restart restores geometry only from current exact measured proof.
        embedder._reset_embedder_state()
        reopened = await asyncio.to_thread(embedder.embedding_metadata)
        assert reopened == metadata
        results, degraded = await asyncio.to_thread(vector_store.search_with_status, "current measured namespace")
        assert not degraded and results[0]["id"] == memory_id
        assert len(calls) == 13
        from src.llm_runtime import completion_with_fallback
        from src.model_fabric.caller_context import build_canonical_inference_context
        from src.observer.screenshot_semantic_analysis import analyze_screenshot_image
        from src.observer.screen_analysis_settings import write_screen_analysis_settings
        write_screen_analysis_settings({"enabled": True})
        image = model_fabric_workspace / "native-fixture.png"
        image.write_bytes(api._ONE_PIXEL_PNG)
        messages = [{"role": "user", "content": "bounded native chat fixture"}]
        chat_context = build_canonical_inference_context("chat_agent", payload=messages,
            output_tokens=64, timeout_seconds=45, principal=operator.principal, session_id=operator.session_id)
        hold_embedding[0] = True
        embedding_task = asyncio.create_task(asyncio.to_thread(embedder.embed, "native embedding fixture", principal=operator.principal))
        chat_task = vision_task = None
        try:
            assert await asyncio.to_thread(embedding_started.wait, 5)
            # Both are accepted while embedding occupies the sole serial lane.
            vision_task = asyncio.create_task(analyze_screenshot_image(image, {"created_at": "2026-10-05T00:00:00Z"}))
            chat_task = asyncio.create_task(completion_with_fallback(messages=messages, temperature=0,
                max_tokens=64, runtime_path="chat_agent", request_context=chat_context))
            for _ in range(100):
                pending = (await durable_job_repository.inference_accounting_snapshot())["operations"]
                if len(pending) == 16:
                    break
                await asyncio.sleep(0.01)
            assert len(pending) == 16
        finally:
            release_embedding.set()
            native_results = await asyncio.wait_for(asyncio.gather(*(task for task in (embedding_task, chat_task, vision_task) if task is not None)), 10)
            hold_embedding[0] = False
        assert len(native_results[0]) == 3
        assert native_results[1].choices[0].message.content
        assert native_results[2].summary == "The operator is reviewing a Seraph test."
        assert ordinary_order == ["fixture/embedding", "fixture/text", "fixture/vision"]
        assert contact_state["peak"] == 1 and contact_state["active"] == 0
        snapshot = await durable_job_repository.inference_accounting_snapshot()
        assert snapshot["committed_microusd"] == 16 and snapshot["unknown_microusd"] == 0
        assert len(calls) == 16
        # A new target without proof cannot query the prior namespace.
        changed = _v2_setup_payload()
        changed["routes"]["embedding"]["model_id"] = "fixture/new-embedding"
        saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3, "openrouter_setup": changed})
        assert saved.status_code == 200
        assert await asyncio.to_thread(vector_store.search_with_status, "old namespace forbidden") == ([], True)
        assert len(calls) == 16
        assert vector_store._get_db().table_names() == [table_name]
    finally:
        reset_runtime_context(tokens)
        embedder._reset_embedder_state()


@pytest.mark.parametrize("vector", ([True], [float("nan")], [float("inf")], [0.0], [10**1000], [], [0.1] * 65_537))
def test_embedding_canary_rejects_unmeasured_or_unbounded_geometry(vector):
    from src.api.model_fabric_settings import _embedding_canary_dimension
    assert _embedding_canary_dimension({"data": [{"index": 0, "embedding": vector}]}) is None


@pytest.fixture
def keyless_openrouter(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_keyless_setup_is_persisted_and_status_is_configuration_required(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-keyless-sentinel-must-not-appear"
    with patch(
        "src.api.model_fabric_settings.httpx.AsyncClient",
        side_effect=AssertionError("settings must not invoke provider transport"),
    ):
        response = await client.put(
            "/api/settings/model-fabric",
            json={"openrouter": _setup_payload()},
        )
        assert response.status_code == 200
        status = await client.get("/api/settings/model-fabric")

    assert status.status_code == 200
    body = status.json()
    setup = body["openrouter_setup"]
    assert body["status"] == "configuration_required"
    assert setup["status"] == "configuration_required"
    assert setup["credential_configured"] is False
    assert setup["credential_ref"] == OPENROUTER_VAULT_CREDENTIAL_REF
    assert setup["api_base"] == "https://openrouter.ai/api/v1"
    assert "api_key" not in body
    assert sentinel not in response.text
    assert sentinel not in status.text

    persisted = Path(settings.workspace_dir) / "model-fabric-settings.json"
    persisted_text = persisted.read_text(encoding="utf-8")
    assert sentinel not in persisted_text
    assert "openrouter" in body["persisted_profile_ids"]
    profile = next(item for item in body["profiles"] if item["id"] == "openrouter")
    assert profile["api_base"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_secret_input_is_vault_backed_and_only_fingerprint_is_returned(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-test-openrouter-write-only"
    with patch(
        "src.api.model_fabric_settings.httpx.AsyncClient",
        side_effect=AssertionError("settings must not invoke provider transport"),
    ):
        response = await client.put(
            "/api/settings/model-fabric",
            json={"openrouter": _setup_payload(api_key=sentinel)},
        )

    assert response.status_code == 200
    body = response.json()
    setup = body["openrouter_setup"]
    assert setup["credential_configured"] is True
    assert setup["credential_ref"] == OPENROUTER_VAULT_CREDENTIAL_REF
    assert setup["credential_fingerprint"]
    assert sentinel not in response.text
    assert '"api_key"' not in response.text
    assert await vault_repository.get("openrouter_api_key") == sentinel

    config_text = (
        Path(settings.workspace_dir) / "model-fabric-settings.json"
    ).read_text(encoding="utf-8")
    assert sentinel not in config_text


@pytest.mark.asyncio
async def test_persisted_controls_drive_profile_and_remote_admission(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    response = await client.put(
        "/api/settings/model-fabric",
        json={"openrouter": _setup_payload()},
    )
    assert response.status_code == 200

    profile = provider_profiles()["openrouter"]
    controls = profile.options["_seraph_openrouter"]
    assert controls == {
        "model_ids": ["openrouter/anthropic/claude-sonnet-4"],
        "temperature": 0.4,
        "output_limit": 2048,
        "timeout_seconds": 45.0,
        "allowed_upstreams": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "data_retention_policy": "deny",
        "zero_data_retention": False,
        "egress_class": "cloud_allowed_full",
        "cloud_egress_acknowledged": True,
        "max_queued": 8,
        "max_inflight": 1,
        "max_outstanding_per_owner": 4,
        "max_retries": 1,
        "spend_ceiling_microusd": 25_000,
    }
    admission = await remote_inference_admission_broker.status()
    assert admission["max_inflight"] == 1
    assert admission["max_retries"] == 1
    assert admission["capacity"]["max_queued"] == 8
    assert admission["capacity"]["max_outstanding_per_owner"] == 4
    assert admission["capacity"]["max_owner_cost_microusd"] == 25_000
    settings.openrouter_api_key = "test-key"
    runtime_kwargs = build_completion_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        temperature=1.9,
        max_tokens=9999,
        profile="openrouter",
    )
    assert runtime_kwargs["model"] == "openrouter/anthropic/claude-sonnet-4"
    assert runtime_kwargs["temperature"] == 0.4
    assert runtime_kwargs["max_tokens"] == 2048
    assert runtime_kwargs["timeout"] == 45.0
    assert "_seraph_openrouter" not in runtime_kwargs


@pytest.mark.asyncio
async def test_invalid_setup_does_not_store_supplied_secret(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-invalid-policy-must-not-be-stored"
    response = await client.put(
        "/api/settings/model-fabric",
        json={
            "openrouter": _setup_payload(
                allow_fallbacks=True,
                api_key=sentinel,
            )
        },
    )
    assert response.status_code == 422
    assert await vault_repository.get("openrouter_api_key") is None


@pytest.mark.asyncio
async def test_vault_credential_hydrates_before_profile_key_resolution(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    sentinel = "sk-restart-hydration-sentinel"
    response = await client.put(
        "/api/settings/model-fabric",
        json={"openrouter": _setup_payload(api_key=sentinel)},
    )
    assert response.status_code == 200
    settings.openrouter_api_key = ""

    assert await hydrate_openrouter_credential() is True
    assert _profile_api_key("openrouter") == sentinel
    status = await client.get("/api/settings/model-fabric")
    assert sentinel not in status.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("allow_fallbacks", True, "fallbacks"),
        ("capabilities", ["audio"], "capability"),
        ("model_ids", ["anthropic/claude-sonnet-4", "z-ai/glm-5.3-flash"], "multiple openrouter model"),
        ("max_queued", 65, "less than or equal to 64"),
        ("temperature", 2.1, "less than or equal to 2"),
    ),
)
async def test_setup_rejects_unsafe_policy_and_limits(
    client,
    model_fabric_workspace,
    keyless_openrouter,
    field,
    value,
    message,
):
    response = await client.put(
        "/api/settings/model-fabric",
        json={"openrouter": _setup_payload(**{field: value})},
    )
    assert response.status_code == 422
    assert message.lower() in str(response.json()["detail"]).lower()
    assert not (Path(settings.workspace_dir) / "model-fabric-settings.json").exists()


@pytest.mark.asyncio
async def test_setup_rejects_endpoint_injection_and_missing_cloud_ack(
    client,
    model_fabric_workspace,
    keyless_openrouter,
):
    endpoint_injection = await client.put(
        "/api/settings/model-fabric",
        json={
            "openrouter": {
                **_setup_payload(),
                "api_base": "https://evil.example/v1",
            }
        },
    )
    assert endpoint_injection.status_code == 422

    missing_ack = await client.put(
        "/api/settings/model-fabric",
        json={
            "openrouter": _setup_payload(cloud_egress_acknowledged=False),
        },
    )
    assert missing_ack.status_code == 422
    assert "acknowledgement" in str(missing_ack.json()["detail"]).lower()
