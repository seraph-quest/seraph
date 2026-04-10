from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import settings


def _write_automation_pack(workspace):
    package_dir = workspace / "extensions" / "openclaw-automation"
    (package_dir / "automation").mkdir(parents=True)
    package_dir.joinpath("manifest.yaml").write_text(
        "id: seraph.openclaw-automation\n"
        "version: 2026.3.24\n"
        "display_name: OpenClaw Automation\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  automation_triggers:\n"
        "    - automation/webhook.yaml\n"
        "    - automation/poll.yaml\n"
        "    - automation/pubsub.yaml\n",
        encoding="utf-8",
    )
    package_dir.joinpath("automation", "webhook.yaml").write_text(
        "name: inbound-webhook\n"
        "description: Signed inbound webhook.\n"
        "trigger_type: webhook\n"
        "endpoint: /api/automation/webhooks/inbound-webhook\n"
        "enabled: true\n"
        "config_fields:\n"
        "  - key: signing_secret\n"
        "    label: Signing Secret\n"
        "    input: password\n"
        "    required: true\n",
        encoding="utf-8",
    )
    package_dir.joinpath("automation", "poll.yaml").write_text(
        "name: remote-poll\n"
        "description: Remote poll watcher.\n"
        "trigger_type: poll\n"
        "schedule: \"*/5 * * * *\"\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    package_dir.joinpath("automation", "pubsub.yaml").write_text(
        "name: guardian-topic\n"
        "description: Guarded pubsub topic.\n"
        "trigger_type: pubsub\n"
        "topic: seraph.guardian\n"
        "enabled: false\n",
        encoding="utf-8",
    )
    return package_dir


def _write_duplicate_webhook_pack(workspace, *, package_name: str, extension_id: str):
    package_dir = workspace / "extensions" / package_name
    (package_dir / "automation").mkdir(parents=True)
    package_dir.joinpath("manifest.yaml").write_text(
        f"id: {extension_id}\n"
        "version: 2026.3.24\n"
        "display_name: Duplicate Webhook\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  automation_triggers:\n"
        "    - automation/shared.yaml\n",
        encoding="utf-8",
    )
    package_dir.joinpath("automation", "shared.yaml").write_text(
        "name: shared-hook\n"
        "description: Shared webhook.\n"
        "trigger_type: webhook\n"
        "endpoint: /api/automation/webhooks/shared-hook\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    return package_dir


@pytest.mark.asyncio
async def test_automation_trigger_inventory_lists_runtime_states(client, tmp_path):
    workspace = tmp_path / "workspace"
    package_dir = _write_automation_pack(workspace)
    (workspace / "extensions-state.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "seraph.openclaw-automation": {
                        "config": {
                            "automation_triggers": {
                                "inbound-webhook": {"signing_secret": "top-secret"},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/automation/triggers")

    assert response.status_code == 200
    triggers = {item["name"]: item for item in response.json()["triggers"]}
    assert triggers["inbound-webhook"]["runtime_state"] == "armed_webhook"
    assert triggers["inbound-webhook"]["endpoint"] == "/api/automation/webhooks/inbound-webhook"
    assert triggers["remote-poll"]["runtime_state"] == "staged_runtime"
    assert triggers["remote-poll"]["endpoint"] == ""
    assert triggers["guardian-topic"]["runtime_state"] == "disabled"


@pytest.mark.asyncio
async def test_webhook_trigger_accepts_signed_payload(client, tmp_path):
    workspace = tmp_path / "workspace"
    _write_automation_pack(workspace)
    (workspace / "extensions-state.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "seraph.openclaw-automation": {
                        "config": {
                            "automation_triggers": {
                                "inbound-webhook": {"signing_secret": "top-secret"},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with (
        patch.object(settings, "workspace_dir", str(workspace)),
        patch("src.api.automation.log_integration_event", AsyncMock()),
    ):
        rejected = await client.post(
            "/api/automation/webhooks/inbound-webhook",
            headers={"X-Seraph-Signature": "wrong"},
            json={"kind": "sync"},
        )
        accepted = await client.post(
            "/api/automation/webhooks/inbound-webhook",
            headers={"X-Seraph-Signature": "top-secret"},
            json={"kind": "sync"},
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["status"] == "accepted"
    assert payload["trigger"]["name"] == "inbound-webhook"
    assert payload["payload"] == {"kind": "sync"}


@pytest.mark.asyncio
async def test_webhook_trigger_requires_signing_secret_even_when_pack_omits_config_fields(client, tmp_path):
    workspace = tmp_path / "workspace"
    package_dir = workspace / "extensions" / "unsigned-webhook"
    (package_dir / "automation").mkdir(parents=True)
    package_dir.joinpath("manifest.yaml").write_text(
        "id: seraph.unsigned-webhook\n"
        "version: 2026.3.24\n"
        "display_name: Unsigned Webhook\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  automation_triggers:\n"
        "    - automation/webhook.yaml\n",
        encoding="utf-8",
    )
    package_dir.joinpath("automation", "webhook.yaml").write_text(
        "name: unsigned-hook\n"
        "description: Missing explicit config fields.\n"
        "trigger_type: webhook\n"
        "endpoint: /api/automation/webhooks/unsigned-hook\n"
        "enabled: true\n",
        encoding="utf-8",
    )

    with patch.object(settings, "workspace_dir", str(workspace)):
        inventory_response = await client.get("/api/automation/triggers")
        webhook_response = await client.post("/api/automation/webhooks/unsigned-hook", json={"kind": "sync"})

    assert inventory_response.status_code == 200
    triggers = {item["name"]: item for item in inventory_response.json()["triggers"]}
    assert triggers["unsigned-hook"]["configured"] is False
    assert triggers["unsigned-hook"]["runtime_state"] == "requires_config"
    assert "signing_secret" not in triggers["unsigned-hook"]["config_keys"]
    assert webhook_response.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_webhook_trigger_names_do_not_arm_inventory_or_route(client, tmp_path):
    workspace = tmp_path / "workspace"
    _write_duplicate_webhook_pack(workspace, package_name="dup-a", extension_id="seraph.dup-a")
    _write_duplicate_webhook_pack(workspace, package_name="dup-b", extension_id="seraph.dup-b")

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/automation/triggers")
        webhook_response = await client.post("/api/automation/webhooks/shared-hook", json={"kind": "sync"})

    assert response.status_code == 200
    trigger_names = [item["name"] for item in response.json()["triggers"]]
    assert trigger_names == []
    assert webhook_response.status_code == 404
