"""Closed optional NEAR settings with real vault/accounting/witness publication."""

from dataclasses import replace
import json

import pytest

from src.model_fabric.configuration import (
    capture_near_text_credential, read_model_fabric_configuration,
    deployment_spend_ceiling, _configuration_payload,
    write_model_fabric_configuration, effective_workload_policy,
)
from src.model_fabric.effective_policy import current_near_text_policy, current_inference_policy
from src.vault.repository import vault_repository
from tests.test_openrouter_setup_api import model_fabric_workspace, keyless_openrouter, _v2_setup_payload


def near_payload(**changes):
    body = {"enabled": True, "max_output_tokens": 256, "timeout_seconds": 45,
        "request_cost_bound_microusd": 1000, "spend_ceiling_microusd": 25000,
        "plaintext_provider_egress_acknowledged": True}
    body.update(changes)
    return body


async def save(client, revision, **changes):
    return await client.put("/api/settings/model-fabric", json={"expected_policy_revision": revision,
        "near_text": near_payload(**changes)})


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_near_only_bootstrap_and_keyless_readiness(client, model_fabric_workspace, keyless_openrouter):
    response = await save(client, 1)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["openrouter_setup"] is None
    assert result["egress_revision"] == 3
    assert result["near_text"]["status"] == "configuration_required"
    assert result["near_text"]["consent_current"] is True
    assert result["near_text"]["key_present"] is False
    assert result["near_text"]["tee_verified"] is False and result["near_text"]["e2ee"] is False
    assert result["inference_accounting"]["ceiling_microusd"] == 25000
    configured, digest = current_near_text_policy()
    assert len(digest) == 64 and deployment_spend_ceiling(configured) == 25000
    with pytest.raises(PermissionError):
        current_inference_policy()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_omission_preserves_near_and_null_or_simultaneous_save_rejects(client, model_fabric_workspace, keyless_openrouter):
    assert (await save(client, 1, api_key="sk-near-preserved")).status_code == 200
    path = model_fabric_workspace / "model-fabric-settings.json"
    original = path.read_bytes()
    assert (await client.put("/api/settings/model-fabric", json={})).status_code == 200
    assert path.read_bytes() == original
    for field in ("profiles", "workload_policies"):
        assert (await client.put("/api/settings/model-fabric", json={field: []})).status_code == 422
        assert (await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
            "near_text": near_payload(), field: []})).status_code == 422
        assert path.read_bytes() == original
    assert (await client.put("/api/settings/model-fabric", json={"near_text": None})).status_code == 422
    assert (await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_text": near_payload(), "openrouter_setup": _v2_setup_payload()})).status_code == 422
    assert path.read_bytes() == original
    assert await vault_repository.get("near_text_api_key") == "sk-near-preserved"
    canary = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near.text", "capability": "text"})
    assert canary.status_code == 403 and canary.json()["detail"] == "near_text_dedicated_native_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_separate_vault_key_and_capture_revision(client, model_fabric_workspace, keyless_openrouter):
    await vault_repository.store("openrouter_api_key", "sk-or-retained")
    response = await save(client, 1, api_key="sk-near-private")
    assert response.status_code == 200, response.text
    assert "sk-near-private" not in response.text and "sk-or-retained" not in response.text
    near = response.json()["near_text"]
    assert near["status"] == "configured" and near["key_present"]
    assert await vault_repository.get("openrouter_api_key") == "sk-or-retained"
    assert await capture_near_text_credential(expected_revision=3, expected_fingerprint=near["credential_fingerprint"]) == "sk-near-private"
    with pytest.raises(PermissionError):
        await capture_near_text_credential(expected_revision=1, expected_fingerprint=near["credential_fingerprint"])
    assert (await save(client, 3, api_key="", plaintext_provider_egress_acknowledged=False)).status_code == 200
    assert await vault_repository.get("near_text_api_key") == "sk-near-private"
    assert (await save(client, 5, enabled=False, plaintext_provider_egress_acknowledged=False)).status_code == 200
    with pytest.raises(PermissionError):
        current_near_text_policy()
    assert await vault_repository.get("near_text_api_key") == "sk-near-private"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_near_regrant_does_not_resurrect_revoked_or_consent(client, model_fabric_workspace, keyless_openrouter):
    assert (await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "openrouter_setup": _v2_setup_payload(api_key="sk-or-kept")})).status_code == 200
    assert (await save(client, 3, api_key="sk-near-kept")).status_code == 200
    original = read_model_fabric_configuration()
    write_model_fabric_configuration(replace(original, egress_revision=6, egress_revoked=True), expected_revision=5)
    response = await save(client, 6)
    assert response.status_code == 200, response.text
    active = read_model_fabric_configuration()
    assert active.egress_revision == 8 and not active.egress_revoked
    assert active.openrouter_setup.routes == original.openrouter_setup.routes
    assert active.profiles == original.profiles
    assert active.openrouter_setup.cloud_egress_acknowledged is False
    assert active.openrouter_setup.purpose_consents == {}
    assert response.json()["openrouter_setup"]["status"] == "blocked"
    assert response.json()["near_text"]["consent_current"] is True
    assert effective_workload_policy("chat_agent").allowed_profile_ids == ()
    with pytest.raises(PermissionError):
        current_inference_policy()
    assert await vault_repository.get("openrouter_api_key") == "sk-or-kept"
    assert await vault_repository.get("near_text_api_key") == "sk-near-kept"
    write_model_fabric_configuration(replace(active, egress_revision=9, egress_revoked=True), expected_revision=8)
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 9,
        "openrouter_setup": _v2_setup_payload()})
    assert response.status_code == 200, response.text
    assert response.json()["near_text"]["enabled"] is True
    assert response.json()["near_text"]["consent_current"] is False
    assert response.json()["near_text"]["status"] == "blocked"
    with pytest.raises(PermissionError):
        current_near_text_policy()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_disabled_save_keeps_global_revoke_and_vault(client, model_fabric_workspace, keyless_openrouter):
    assert (await save(client, 1, api_key="sk-near-retained")).status_code == 200
    original = read_model_fabric_configuration()
    write_model_fabric_configuration(replace(original, egress_revision=4, egress_revoked=True), expected_revision=3)
    response = await save(client, 4, enabled=False, plaintext_provider_egress_acknowledged=False)
    assert response.status_code == 200, response.text
    assert response.json()["egress_revision"] == 6 and response.json()["egress_revoked"] is True
    assert response.json()["near_text"]["plaintext_egress_consent_revision"] is None
    assert await vault_repository.get("near_text_api_key") == "sk-near-retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("tamper", ("mirror", "malformed_near"))
async def test_persisted_mismatch_and_malformed_near_degrade_without_widening_or(client, model_fabric_workspace, keyless_openrouter, tamper):
    assert (await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "openrouter_setup": _v2_setup_payload()})).status_code == 200
    assert (await save(client, 3)).status_code == 200
    path = model_fabric_workspace / "model-fabric-settings.json"
    payload = json.loads(path.read_text())
    if tamper == "mirror":
        payload["near_text"]["spend_ceiling_microusd"] = 30000
    else:
        payload["openrouter_setup"]["cloud_egress_acknowledged"] = False
        payload["near_text"] = {"enabled": True}
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    configured = read_model_fabric_configuration()
    assert configured.status == "degraded"
    with pytest.raises(PermissionError):
        current_near_text_policy()
    with pytest.raises(PermissionError):
        current_inference_policy()
    assert (await client.get("/api/settings/model-fabric")).status_code == 200
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("change", ({"enabled": 1}, {"max_output_tokens": True}, {"max_output_tokens": 1025},
    {"timeout_seconds": True}, {"timeout_seconds": 46}, {"request_cost_bound_microusd": 0},
    {"request_cost_bound_microusd": 25001}, {"model_id": "other/model"},
    {"api_base": "https://example.com/v1"}, {"credential_ref": "env:OPENROUTER_API_KEY"},
    {"plaintext_egress_consent_revision": 3}, {"plaintext_provider_egress_acknowledged": "true"}))
async def test_closed_near_input_rejects_before_mutation(client, model_fabric_workspace, keyless_openrouter, change):
    response = await save(client, 1, **change)
    assert response.status_code == 422, response.text
    assert not (model_fabric_workspace / "model-fabric-settings.json").exists()
    assert await vault_repository.get("near_text_api_key") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_near_stale_cas_and_missing_ack_do_not_store_key(client, model_fabric_workspace, keyless_openrouter):
    assert (await save(client, 2, api_key="sk-near-stale")).status_code == 409
    assert (await save(client, 1, plaintext_provider_egress_acknowledged=False, api_key="sk-near-stale")).status_code == 403
    assert await vault_repository.get("near_text_api_key") is None
    assert not (model_fabric_workspace / "model-fabric-settings.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_near_preserves_or_slots_and_shared_ceiling(client, model_fabric_workspace, keyless_openrouter):
    response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "openrouter_setup": _v2_setup_payload(api_key="sk-or-original")})
    assert response.status_code == 200, response.text
    original = read_model_fabric_configuration()
    before = (model_fabric_workspace / "model-fabric-settings.json").read_bytes()
    assert (await save(client, 3, spend_ceiling_microusd=30000)).status_code == 409
    assert (model_fabric_workspace / "model-fabric-settings.json").read_bytes() == before
    assert (await save(client, 3, api_key="sk-near-own")).status_code == 200
    combined = read_model_fabric_configuration()
    assert combined.profiles == original.profiles
    assert combined.openrouter_setup.routes == original.openrouter_setup.routes
    assert combined.openrouter_setup.purpose_consents == {"vision": 5, "embedding": 5}
    assert await vault_repository.get("openrouter_api_key") == "sk-or-original"
    changed = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "openrouter_setup": _v2_setup_payload(spend_ceiling_microusd=30000,
            vision_egress_acknowledged=False, embedding_egress_acknowledged=False)})
    assert changed.status_code == 200, changed.text
    assert changed.json()["near_text"]["spend_ceiling_microusd"] == 30000
    assert changed.json()["near_text"]["consent_current"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_near_vault_compensation_after_revoked_publication(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.api import model_fabric_settings as api
    assert (await save(client, 1, api_key="sk-near-original")).status_code == 200
    original = api.write_model_fabric_configuration
    def fail_activation(configuration, **kwargs):
        if not configuration.egress_revoked:
            raise OSError("bounded test publication failure")
        return original(configuration, **kwargs)
    monkeypatch.setattr(api, "write_model_fabric_configuration", fail_activation)
    response = await save(client, 3, api_key="sk-near-replacement")
    assert response.status_code == 503, response.text
    assert await vault_repository.get("near_text_api_key") == "sk-near-original"
    configured = read_model_fabric_configuration()
    assert configured.egress_revoked and configured.egress_revision == 4
    with pytest.raises(PermissionError):
        current_near_text_policy()
