from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from config.settings import settings


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
        "  seraph: \">=2026.4.10\"\n"
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
        "  seraph: \">=2026.4.10\"\n"
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


@pytest.mark.asyncio
async def test_browser_session_rest_surface_remains_unmounted(client):
    list_response = await client.get("/api/browser/sessions")
    assert list_response.status_code == 404

    get_response = await client.get("/api/browser/sessions/bs-123")
    assert get_response.status_code == 404

    delete_response = await client.delete("/api/browser/sessions/bs-123")
    assert delete_response.status_code == 404


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
            "  seraph: \">=2026.4.10\"\n"
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
