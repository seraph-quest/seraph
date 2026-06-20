"""Tests for preserved screen capture artifact inspection endpoints."""

import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from src.db.models import ScreenObservation


@pytest.mark.asyncio
async def test_screen_artifacts_are_persisted_listed_and_served(async_db, client, tmp_path, monkeypatch):
    monkeypatch.setattr("src.api.observer.settings.screen_capture_archive_dir", str(tmp_path))
    image_path = tmp_path / "capture.png"
    output_path = tmp_path / "capture.codex.txt"
    analysis_path = tmp_path / "capture.analysis.json"
    image_path.write_bytes(b"png bytes")
    output_path.write_text('{"summary":"Codex saw the editor"}', encoding="utf-8")
    analysis_path.write_text(
        json.dumps({"summary": "Codex saw the editor", "activity": "coding"}),
        encoding="utf-8",
    )

    artifacts = {
        "id": "artifact-1",
        "image_path": str(image_path),
        "codex_output_path": str(output_path),
        "analysis_path": str(analysis_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    resp = await client.post(
        "/api/observer/context",
        json={
            "active_window": "VS Code - seraph",
            "observation": {
                "app": "VS Code",
                "window_title": "seraph",
                "activity": "coding",
                "project": "seraph",
                "summary": "Codex saw the editor",
                "details": ["editing screen artifacts"],
                "capture_artifacts": artifacts,
            },
            "switch_timestamp": 1700000000.0,
        },
    )
    assert resp.status_code == 200

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    stored_details = json.loads(observation.details_json or "[]")
    assert "editing screen artifacts" in stored_details
    assert any(item.startswith("capture_artifacts:") for item in stored_details)

    list_resp = await client.get("/api/observer/screen-artifacts")
    assert list_resp.status_code == 200
    item = list_resp.json()["items"][0]
    assert item["observation_id"] == observation.id
    assert item["artifacts"]["image_url"].endswith(f"/{observation.id}/image")
    assert item["artifacts"]["codex_output_url"].endswith(f"/{observation.id}/codex-output")

    image_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/image")
    assert image_resp.status_code == 200
    assert image_resp.content == b"png bytes"
    assert image_resp.headers["content-type"] == "image/png"

    output_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/codex-output")
    assert output_resp.status_code == 200
    assert output_resp.text == '{"summary":"Codex saw the editor"}'

    analysis_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/analysis")
    assert analysis_resp.status_code == 200
    assert analysis_resp.json()["summary"] == "Codex saw the editor"


@pytest.mark.asyncio
async def test_screen_artifact_endpoints_reject_paths_outside_archive(async_db, client, tmp_path, monkeypatch):
    monkeypatch.setattr("src.api.observer.settings.screen_capture_archive_dir", str(tmp_path))
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"not allowed")
    artifacts = {
        "id": "artifact-2",
        "image_path": str(outside),
        "codex_output_path": str(tmp_path / "missing.txt"),
        "analysis_path": str(tmp_path / "missing.json"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    async with async_db() as db:
        observation = ScreenObservation(
            app_name="VS Code",
            window_title="seraph",
            activity_type="coding",
            summary="Outside artifact path",
            details_json=json.dumps(["capture_artifacts:" + json.dumps(artifacts)]),
        )
        db.add(observation)

    resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/image")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_screen_artifact_endpoints_are_localhost_only(app, async_db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.api.observer.settings.screen_capture_archive_dir", str(tmp_path))
    transport = ASGITransport(app=app, client=("192.0.2.10", 53210))
    async with AsyncClient(transport=transport, base_url="http://test") as remote_client:
        resp = await remote_client.get("/api/observer/screen-artifacts")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Screen artifacts are only available from localhost"
