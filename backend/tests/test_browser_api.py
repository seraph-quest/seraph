from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from config.settings import settings
from src.browser.sessions import browser_session_runtime


@pytest.fixture(autouse=True)
def reset_browser_sessions():
    browser_session_runtime.reset_for_tests()
    yield
    browser_session_runtime.reset_for_tests()


@pytest.mark.asyncio
async def test_browser_provider_inventory_endpoint_lists_staged_remote_modes(client, tmp_path):
    workspace = tmp_path / "workspace"
    remote_pack = workspace / "extensions" / "openclaw-remote-cdp"
    relay_pack = workspace / "extensions" / "openclaw-extension-relay"
    (remote_pack / "connectors" / "browser").mkdir(parents=True)
    (relay_pack / "connectors" / "browser").mkdir(parents=True)
    remote_pack.joinpath("manifest.yaml").write_text(
        "id: seraph.openclaw-remote-cdp\n"
        "version: 2026.3.24\n"
        "display_name: OpenClaw Remote CDP\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.11\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  browser_providers:\n"
        "    - connectors/browser/remote-cdp.yaml\n",
        encoding="utf-8",
    )
    remote_pack.joinpath("connectors", "browser", "remote-cdp.yaml").write_text(
        "name: remote-cdp\n"
        "description: Remote CDP provider.\n"
        "provider_kind: remote_cdp\n"
        "enabled: false\n"
        "credential_surface: remote_cdp_endpoint\n"
        "cookie_scope: remote_profile\n"
        "profile_persistence: provider_managed_ephemeral\n"
        "owner_scope: runtime_session\n"
        "remote_transport: cdp_websocket\n"
        "fallback_policy: local_extract_only\n"
        "config_fields:\n"
        "  - key: ws_endpoint\n"
        "    label: CDP WebSocket Endpoint\n"
        "    input: url\n"
        "    required: true\n",
        encoding="utf-8",
    )
    relay_pack.joinpath("manifest.yaml").write_text(
        "id: seraph.openclaw-extension-relay\n"
        "version: 2026.3.24\n"
        "display_name: OpenClaw Extension Relay\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.11\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  browser_providers:\n"
        "    - connectors/browser/extension-relay.yaml\n",
        encoding="utf-8",
    )
    relay_pack.joinpath("connectors", "browser", "extension-relay.yaml").write_text(
        "name: extension-relay\n"
        "description: Extension relay provider.\n"
        "provider_kind: extension_relay\n"
        "enabled: false\n"
        "config_fields:\n"
        "  - key: relay_token\n"
        "    label: Relay Token\n"
        "    input: password\n"
        "    required: true\n",
        encoding="utf-8",
    )
    (workspace / "extensions-state.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "seraph.openclaw-remote-cdp": {
                        "config": {
                            "browser_providers": {
                                "remote-cdp": {"ws_endpoint": "wss://browser.example.test/devtools"},
                            }
                        },
                        "connector_state": {
                            "connectors/browser/remote-cdp.yaml": {"enabled": True},
                        },
                    },
                    "seraph.openclaw-extension-relay": {
                        "config": {
                            "browser_providers": {
                                "extension-relay": {"relay_token": "secret"},
                            }
                        },
                        "connector_state": {
                            "connectors/browser/extension-relay.yaml": {"enabled": True},
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/browser/providers")

    assert response.status_code == 200
    providers = response.json()["providers"]
    names = {item["name"] for item in providers}
    assert {"local-browser", "remote-cdp", "extension-relay"}.issubset(names)
    local_runtime = next(item for item in providers if item["name"] == "local-browser")
    assert local_runtime["provider_kind"] == "local"
    assert local_runtime["runtime_state"] == "ready"
    assert local_runtime["execution_mode"] == "local_runtime"
    remote_cdp = next(item for item in providers if item["name"] == "remote-cdp")
    assert remote_cdp["provider_kind"] == "remote_cdp"
    assert remote_cdp["runtime_state"] == "staged_local_fallback"
    assert remote_cdp["execution_mode"] == "local_fallback"
    assert remote_cdp["credential_surface"] == "remote_cdp_endpoint"
    assert remote_cdp["cookie_scope"] == "remote_profile"
    assert remote_cdp["profile_persistence"] == "provider_managed_ephemeral"
    assert remote_cdp["owner_scope"] == "runtime_session"
    assert remote_cdp["remote_transport"] == "cdp_websocket"
    assert remote_cdp["fallback_policy"] == "local_extract_only"
    extension_relay = next(item for item in providers if item["name"] == "extension-relay")
    assert extension_relay["provider_kind"] == "extension_relay"
    assert extension_relay["runtime_state"] == "requires_boundary_contract"
    assert extension_relay["execution_mode"] == "boundary_contract_required"


@pytest.mark.asyncio
async def test_browser_session_rest_surface_is_owner_scoped_and_provenanced(client):
    with patch("src.api.browser.browse_webpage", return_value="Example page body"):
        open_response = await client.post(
            "/api/browser/sessions",
            json={
                "owner_session_id": "session-a",
                "url": "https://example.test/research",
                "capture": "extract",
            },
        )

    assert open_response.status_code == 200
    session = open_response.json()["session"]
    assert session["owner_session_id"] == "session-a"
    assert session["provider_name"] == "local-browser"
    assert session["execution_mode"] == "local_runtime"
    assert session["partition_id"].startswith("bp-")
    assert "content" not in session
    assert session["boundary_decisions"]["credential"]["enforced"] is False
    assert session["boundary_decisions"]["network"]["state"] == "site_policy_guarded"
    assert session["boundary_decisions"]["network"]["enforced"] is True
    provenance = session["latest_artifact_provenance"]
    assert provenance["artifact_handle"].startswith("seraph://browser-sessions/")
    assert provenance["artifact_body_digest"]
    assert provenance["raw_artifact_body_exposed"] is False
    assert provenance["safe_receipt"]["raw_artifact_body_exposed"] is False
    assert provenance["contains_cookie"] is False
    assert provenance["contains_secret"] is False

    list_response = await client.get("/api/browser/sessions?owner_session_id=session-a")
    assert list_response.status_code == 200
    assert [item["session_id"] for item in list_response.json()["sessions"]] == [session["session_id"]]

    operator_response = await client.get(
        "/api/operator/browser-computer-use-control?owner_session_id=session-a"
    )
    assert operator_response.status_code == 200
    operator_payload = operator_response.json()
    assert operator_payload["sessions"][0]["session_id"] == session["session_id"]
    assert "safe_browser_automation" in operator_payload["blocked_claims"]

    cross_owner_response = await client.get(
        f"/api/browser/sessions/{session['session_id']}?owner_session_id=session-b"
    )
    assert cross_owner_response.status_code == 404

    ref_response = await client.get(
        f"/api/browser/refs/{session['latest_ref']}?owner_session_id=session-a"
    )
    assert ref_response.status_code == 200
    ref_payload = ref_response.json()["ref"]
    assert ref_payload["content"] == "Example page body"
    assert ref_payload["artifact_provenance"]["raw_artifact_body_exposed"] is True
    assert ref_payload["artifact_provenance"]["safe_receipt"]["raw_artifact_body_exposed"] is True


@pytest.mark.asyncio
async def test_browser_session_quarantine_blocks_snapshot_and_replay(client):
    with patch("src.api.browser.browse_webpage", return_value="Example page body"):
        open_response = await client.post(
            "/api/browser/sessions",
            json={"owner_session_id": "session-q", "url": "https://example.test/research"},
        )
    session_id = open_response.json()["session"]["session_id"]

    quarantine_response = await client.post(
        f"/api/browser/sessions/{session_id}/control",
        json={
            "owner_session_id": "session-q",
            "action": "quarantine",
            "reason": "hostile-page prompt injection",
        },
    )
    assert quarantine_response.status_code == 200
    assert quarantine_response.json()["session"]["status"] == "quarantined"
    assert quarantine_response.json()["session"]["recovery_state"] == "operator_review_required"

    with patch("src.api.browser.browse_webpage", return_value="Should not persist"):
        snapshot_response = await client.post(
            f"/api/browser/sessions/{session_id}/snapshot",
            json={"owner_session_id": "session-q", "capture": "extract"},
        )
    assert snapshot_response.status_code == 409
    assert snapshot_response.json()["detail"]["error"] == "session_quarantined"

    replay_response = await client.post(
        f"/api/browser/sessions/{session_id}/control",
        json={"owner_session_id": "session-q", "action": "replay_snapshot"},
    )
    assert replay_response.status_code == 409
    assert replay_response.json()["detail"]["error"] == "session_quarantined"

    recover_response = await client.post(
        f"/api/browser/sessions/{session_id}/control",
        json={"owner_session_id": "session-q", "action": "recover"},
    )
    assert recover_response.status_code == 200
    assert recover_response.json()["session"]["status"] == "open"


@pytest.mark.asyncio
async def test_browser_session_degraded_fallback_replay_requires_acknowledgement(client, tmp_path):
    workspace = tmp_path / "workspace"
    remote_pack = workspace / "extensions" / "openclaw-remote-cdp"
    (remote_pack / "connectors" / "browser").mkdir(parents=True)
    remote_pack.joinpath("manifest.yaml").write_text(
        "id: seraph.openclaw-remote-cdp\n"
        "version: 2026.3.24\n"
        "display_name: OpenClaw Remote CDP\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.11\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  browser_providers:\n"
        "    - connectors/browser/remote-cdp.yaml\n",
        encoding="utf-8",
    )
    remote_pack.joinpath("connectors", "browser", "remote-cdp.yaml").write_text(
        "name: remote-cdp\n"
        "description: Remote CDP provider.\n"
        "provider_kind: remote_cdp\n"
        "enabled: true\n"
        "credential_surface: remote_cdp_endpoint\n"
        "cookie_scope: remote_profile\n"
        "profile_persistence: provider_managed_ephemeral\n"
        "owner_scope: runtime_session\n"
        "remote_transport: cdp_websocket\n"
        "fallback_policy: local_extract_only\n"
        "config_fields:\n"
        "  - key: ws_endpoint\n"
        "    label: CDP WebSocket Endpoint\n"
        "    input: url\n"
        "    required: true\n",
        encoding="utf-8",
    )
    (workspace / "extensions-state.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "seraph.openclaw-remote-cdp": {
                        "config": {
                            "browser_providers": {
                                "remote-cdp": {"ws_endpoint": "wss://browser.example.test/devtools"},
                            }
                        },
                        "connector_state": {
                            "connectors/browser/remote-cdp.yaml": {"enabled": True},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        with patch("src.api.browser.browse_webpage", return_value="Remote provider fallback body"):
            open_response = await client.post(
                "/api/browser/sessions",
                json={
                    "owner_session_id": "session-r",
                    "url": "https://example.test/research",
                    "provider": "remote-cdp",
                },
            )

    assert open_response.status_code == 200
    session = open_response.json()["session"]
    assert session["execution_mode"] == "local_fallback"
    assert session["provider_degradation"]["fallback_labeled"] is True
    assert session["provider_degradation"]["silent_fallback_allowed"] is False

    replay_without_ack = await client.post(
        f"/api/browser/sessions/{session['session_id']}/control",
        json={"owner_session_id": "session-r", "action": "replay_snapshot"},
    )
    assert replay_without_ack.status_code == 409
    assert replay_without_ack.json()["detail"]["error"] == "degraded_fallback_acknowledgement_required"

    with patch.object(settings, "workspace_dir", str(workspace)):
        with patch("src.api.browser.browse_webpage", return_value="Error: replay provider unavailable"):
            failed_replay = await client.post(
                f"/api/browser/sessions/{session['session_id']}/control",
                json={
                    "owner_session_id": "session-r",
                    "action": "replay_snapshot",
                    "acknowledge_degraded_fallback": True,
                },
            )
    assert failed_replay.status_code == 400
    failed_state = await client.get(
        f"/api/browser/sessions/{session['session_id']}?owner_session_id=session-r"
    )
    assert failed_state.status_code == 200
    failed_session = failed_state.json()["session"]
    assert failed_session["snapshot_count"] == 1
    assert all(
        event["action"] != "replay_snapshot"
        for event in failed_session["control_events"]
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        with patch("src.api.browser.browse_webpage", return_value="Acknowledged replay body"):
            replay_with_ack = await client.post(
                f"/api/browser/sessions/{session['session_id']}/control",
                json={
                    "owner_session_id": "session-r",
                    "action": "replay_snapshot",
                    "acknowledge_degraded_fallback": True,
                },
            )
    assert replay_with_ack.status_code == 200
    assert replay_with_ack.json()["session"]["snapshot_count"] == 2
    assert replay_with_ack.json()["event"]["status"] == "applied"
    assert replay_with_ack.json()["session"]["recovery_state"] == "replay_snapshot_recorded"


@pytest.mark.asyncio
async def test_duplicate_browser_provider_names_keep_only_priority_winner_visible(client, tmp_path):
    workspace = tmp_path / "workspace"
    first_pack = workspace / "extensions" / "browser-a"
    second_pack = workspace / "extensions" / "browser-z"
    for package_dir, extension_id, display_name, description in (
        (first_pack, "seraph.browser-a", "A Browser Winner", "Priority winner."),
        (second_pack, "seraph.browser-z", "Z Browser Loser", "Lower priority loser."),
    ):
        (package_dir / "connectors" / "browser").mkdir(parents=True)
        package_dir.joinpath("manifest.yaml").write_text(
            f"id: {extension_id}\n"
            "version: 2026.3.24\n"
            f"display_name: {display_name}\n"
            "kind: connector-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.4.11\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: local\n"
            "contributes:\n"
            "  browser_providers:\n"
            "    - connectors/browser/shared.yaml\n",
            encoding="utf-8",
        )
        package_dir.joinpath("connectors", "browser", "shared.yaml").write_text(
            "name: shared-provider\n"
            f"description: {description}\n"
            "provider_kind: remote_cdp\n"
            "enabled: true\n"
            "config_fields:\n"
            "  - key: ws_endpoint\n"
            "    label: CDP WebSocket Endpoint\n"
            "    input: url\n"
            "    required: true\n",
            encoding="utf-8",
        )

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/browser/providers")

    assert response.status_code == 200
    shared = [item for item in response.json()["providers"] if item["name"] == "shared-provider"]
    assert len(shared) == 1
    assert shared[0]["extension_id"] == "seraph.browser-a"
    assert shared[0]["description"] == "Priority winner."
