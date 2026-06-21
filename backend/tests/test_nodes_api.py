from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from config.settings import settings


def _write_node_pack(workspace):
    package_dir = workspace / "extensions" / "openclaw-node"
    (package_dir / "connectors" / "nodes").mkdir(parents=True)
    package_dir.joinpath("manifest.yaml").write_text(
        "id: seraph.openclaw-node\n"
        "version: 2026.3.24\n"
        "display_name: OpenClaw Node\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.11\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  node_adapters:\n"
        "    - connectors/nodes/companion.yaml\n"
        "    - connectors/nodes/canvas.yaml\n",
        encoding="utf-8",
    )
    package_dir.joinpath("connectors", "nodes", "companion.yaml").write_text(
        "name: companion-node\n"
        "description: Companion node link.\n"
        "adapter_kind: companion\n"
        "enabled: true\n"
        "config_fields:\n"
        "  - key: node_url\n"
        "    label: Node URL\n"
        "    input: url\n"
        "    required: true\n",
        encoding="utf-8",
    )
    package_dir.joinpath("connectors", "nodes", "canvas.yaml").write_text(
        "name: canvas-node\n"
        "description: Canvas adapter.\n"
        "adapter_kind: canvas\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    package_dir.joinpath("connectors", "nodes", "device.yaml").write_text(
        "name: openclaw-device\n"
        "description: Device adapter for staged hardware reach.\n"
        "adapter_kind: device\n"
        "enabled: false\n"
        "capabilities:\n"
        "  - capture\n"
        "  - notify\n"
        "config_fields:\n"
        "  - key: node_url\n"
        "    label: Node URL\n"
        "    input: url\n"
        "    required: true\n",
        encoding="utf-8",
    )
    manifest_path = package_dir.joinpath("manifest.yaml")
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "    - connectors/nodes/device.yaml\n",
        encoding="utf-8",
    )
    return package_dir


@pytest.mark.asyncio
async def test_node_adapter_inventory_lists_staged_and_canvas_states(client, tmp_path):
    workspace = tmp_path / "workspace"
    _write_node_pack(workspace)
    (workspace / "extensions-state.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "seraph.openclaw-node": {
                        "config": {
                            "node_adapters": {
                                "companion-node": {"node_url": "https://nodes.example.test"},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/nodes/adapters")

    assert response.status_code == 200
    adapters = {item["name"]: item for item in response.json()["adapters"]}
    assert adapters["companion-node"]["runtime_state"] == "staged_link"
    assert adapters["companion-node"]["configured"] is True
    assert adapters["companion-node"]["requires_network"] is True
    assert adapters["canvas-node"]["runtime_state"] == "staged_canvas"
    assert adapters["canvas-node"]["requires_network"] is False


@pytest.mark.asyncio
async def test_packaged_device_adapter_defaults_unpaired_staged_and_fail_closed(client, tmp_path):
    workspace = tmp_path / "workspace"
    _write_node_pack(workspace)

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/nodes/adapters")

    assert response.status_code == 200
    adapters = {item["name"]: item for item in response.json()["adapters"]}
    device = adapters["openclaw-device"]
    assert device["runtime_state"] == "unpaired_staged"
    assert device["pairing_state"] == "unpaired"
    assert device["trust_state"] == "unpaired"
    assert device["paired"] is False
    assert device["safe_follow_up_ready"] is False

    contract = device["presence_contract"]
    assert contract["schema"] == "seraph.m4.presence_boundary.v1"
    assert contract["identity"]["adapter_kind"] == "device"
    assert contract["mutation"]["live_transport_claimed"] is False
    assert contract["mutation"]["live_reach_claimed"] is False
    assert contract["mutation"]["follow_up_ready"] is False
    assert contract["notification"]["can_notify"] is False
    for boundary in ("inbound", "outbound", "reply", "workflow", "device_action"):
        assert contract["mutation"]["boundaries"][boundary]["allowed"] is False
        assert contract["mutation"]["boundaries"][boundary]["live"] is False
    assert contract["mutation"]["boundaries"]["approval"]["required"] is True


@pytest.mark.asyncio
async def test_device_pairing_metadata_is_visible_and_revocation_fails_closed(client, tmp_path):
    workspace = tmp_path / "workspace"
    _write_node_pack(workspace)
    (workspace / "extensions-state.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "seraph.openclaw-node": {
                        "node_pairings": {
                            "connectors/nodes/device.yaml": {
                                "pairing_id": "pair-openclaw-1",
                                "device_id": "device-1",
                                "label": "Bench device",
                                "paired_at": "2026-05-05T10:00:00+00:00",
                                "trusted": True,
                                "scopes": ["notify", "capture"],
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        pairings = await client.get("/api/nodes/pairings")

    assert pairings.status_code == 200
    device = next(item for item in pairings.json()["pairings"] if item["name"] == "openclaw-device")
    assert device["runtime_state"] == "paired_staged"
    assert device["pairing_state"] == "paired"
    assert device["trust_state"] == "trusted"
    assert device["safe_follow_up_ready"] is True
    assert device["pairing"]["pairing_id"] == "pair-openclaw-1"
    assert device["presence_contract"]["mutation"]["live_transport_claimed"] is False
    assert device["presence_contract"]["mutation"]["boundaries"]["device_action"]["allowed"] is True
    assert device["presence_contract"]["mutation"]["boundaries"]["device_action"]["live"] is False

    with patch.object(settings, "workspace_dir", str(workspace)):
        revoked = await client.post(
            "/api/nodes/pairings/revoke",
            json={
                "extension_id": "seraph.openclaw-node",
                "reference": "connectors/nodes/device.yaml",
                "reason": "operator revoked trust",
            },
        )

    assert revoked.status_code == 200
    revoked_device = revoked.json()["adapter"]
    assert revoked_device["runtime_state"] == "revoked"
    assert revoked_device["pairing_state"] == "revoked"
    assert revoked_device["trust_state"] == "untrusted"
    assert revoked_device["revoked"] is True
    assert revoked_device["safe_follow_up_ready"] is False
    assert revoked_device["presence_contract"]["trust"]["trusted"] is False
    assert revoked_device["presence_contract"]["mutation"]["follow_up_ready"] is False
    assert revoked_device["presence_contract"]["notification"]["can_notify"] is False
    for boundary in ("inbound", "outbound", "reply", "workflow", "device_action"):
        assert revoked_device["presence_contract"]["mutation"]["boundaries"][boundary]["allowed"] is False
        assert revoked_device["presence_contract"]["mutation"]["boundaries"][boundary]["live"] is False

    state_payload = json.loads((workspace / "extensions-state.json").read_text(encoding="utf-8"))
    state_pairing = state_payload["extensions"]["seraph.openclaw-node"]["node_pairings"]["connectors/nodes/device.yaml"]
    assert state_pairing["revoked"] is True
    assert state_pairing["trust_state"] == "untrusted"

    with patch.object(settings, "workspace_dir", str(workspace)):
        cleared = await client.post(
            "/api/nodes/pairings/clear",
            json={
                "extension_id": "seraph.openclaw-node",
                "reference": "connectors/nodes/device.yaml",
            },
        )

    assert cleared.status_code == 200
    cleared_device = cleared.json()["adapter"]
    assert cleared.json()["cleared"] is True
    assert cleared_device["pairing_state"] == "unpaired"
    assert cleared_device["trust_state"] == "unpaired"
    assert cleared_device["safe_follow_up_ready"] is False
