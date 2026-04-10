from __future__ import annotations

from unittest.mock import patch

import pytest

from config.settings import settings


def _write_canvas_pack(workspace):
    package_dir = workspace / "extensions" / "openclaw-canvas"
    (package_dir / "canvas").mkdir(parents=True)
    package_dir.joinpath("manifest.yaml").write_text(
        "id: seraph.openclaw-canvas\n"
        "version: 2026.3.24\n"
        "display_name: OpenClaw Canvas\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  canvas_outputs:\n"
        "    - canvas/guardian-board.yaml\n",
        encoding="utf-8",
    )
    package_dir.joinpath("canvas", "guardian-board.yaml").write_text(
        "name: guardian-board\n"
        "title: Guardian Board\n"
        "description: Structured board for workflow runs.\n"
        "surface_kind: board\n"
        "sections:\n"
        "  - Summary\n"
        "  - Steps\n"
        "artifact_types:\n"
        "  - note\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_canvas_output_inventory_lists_structured_surfaces(client, tmp_path):
    workspace = tmp_path / "workspace"
    _write_canvas_pack(workspace)

    with patch.object(settings, "workspace_dir", str(workspace)):
        response = await client.get("/api/canvas/outputs")

    assert response.status_code == 200
    outputs = {item["name"]: item for item in response.json()["outputs"]}
    assert outputs["guardian-board"]["title"] == "Guardian Board"
    assert outputs["guardian-board"]["surface_kind"] == "board"
    assert outputs["guardian-board"]["sections"] == ["Summary", "Steps"]
