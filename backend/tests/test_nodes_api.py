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
        "  seraph: \">=2026.4.10\"\n"
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
