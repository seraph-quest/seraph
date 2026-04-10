from __future__ import annotations

from unittest.mock import patch

import pytest

from config.settings import settings


def _write_workflow_runtime_pack(workspace):
    package_dir = workspace / "extensions" / "openclaw-runtimes"
    (package_dir / "runtimes").mkdir(parents=True)
    package_dir.joinpath("manifest.yaml").write_text(
        "id: seraph.openclaw-runtimes\n"
        "version: 2026.3.24\n"
        "display_name: OpenClaw Runtimes\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  workflow_runtimes:\n"
        "    - runtimes/openprose.yaml\n",
        encoding="utf-8",
    )
    package_dir.joinpath("runtimes", "openprose.yaml").write_text(
        "name: openprose\n"
        "engine_kind: openprose\n"
        "description: Narrative drafting runtime.\n"
        "delegation_mode: inline\n"
        "checkpoint_policy: step\n"
        "structured_output: true\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_workflow_runtimes_inventory_lists_profiles(client, tmp_path):
    workspace = tmp_path / "workspace"
    _write_workflow_runtime_pack(workspace)

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/workflows/runtimes")

    assert response.status_code == 200
    runtimes = {item["name"]: item for item in response.json()["runtimes"]}
    assert runtimes["openprose"]["engine_kind"] == "openprose"
    assert runtimes["openprose"]["structured_output"] is True
