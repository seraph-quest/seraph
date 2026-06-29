"""Tests for preserved screen capture artifact inspection endpoints."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from src.db.models import ScreenObservation


@pytest.mark.asyncio
async def test_screen_artifacts_are_persisted_listed_and_served(async_db, client, tmp_path, monkeypatch):
    monkeypatch.setattr("src.api.observer.settings.workspace_dir", str(tmp_path / "workspace"))
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
    assert item["artifacts"]["provider_output_url"].endswith(f"/{observation.id}/codex-output")

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


@pytest.mark.asyncio
async def test_screen_artifact_root_prefers_seraph_archive_env(tmp_path, monkeypatch):
    from src.api.observer import _screen_artifact_root

    preferred = tmp_path / "seraph-screen"
    fallback = tmp_path / "fallback-screen"
    monkeypatch.setattr("src.api.observer.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setenv("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR", str(preferred))
    monkeypatch.setattr("src.api.observer.settings.screen_capture_archive_dir", str(fallback))

    assert _screen_artifact_root() == preferred.resolve()


@pytest.mark.asyncio
async def test_screen_artifact_root_prefers_screen_analysis_settings(tmp_path, monkeypatch):
    from src.api.observer import _screen_artifact_root

    workspace = tmp_path / "workspace"
    preferred = tmp_path / "settings-screen"
    env_fallback = tmp_path / "env-screen"
    workspace.mkdir()
    (workspace / "screen-analysis-settings.json").write_text(
        json.dumps({"archive_dir": str(preferred)}),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.api.observer.settings.workspace_dir", str(workspace))
    monkeypatch.setenv("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR", str(env_fallback))

    assert _screen_artifact_root() == preferred.resolve()


@pytest.mark.asyncio
async def test_framekeeper_manifest_ingest_persists_observation_and_serves_image(
    async_db,
    client,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "framekeeper"
    seraph_archive = tmp_path / "seraph-screen"
    monkeypatch.setattr("src.api.settings.settings.workspace_dir", str(tmp_path / "workspace"))
    monkeypatch.setattr("src.api.observer.settings.workspace_dir", str(tmp_path / "workspace"))
    image = _write_framekeeper_capture(root, capture_id="capture-valid")
    await client.put(
        "/api/settings/screen-analysis",
        json={"archive_dir": str(seraph_archive)},
    )

    resp = await client.post(
        "/api/observer/framekeeper/ingest",
        json={"artifact_root": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scanned"] == 1
    assert payload["ingested"] == 1
    assert payload["rejected"] == []

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    assert observation.app_name == "VS Code"
    assert observation.summary == "Framekeeper captured VS Code: framekeeper_source.py."
    stored_details = json.loads(observation.details_json or "[]")
    assert "framekeeper_capture_id:capture-valid" in stored_details
    artifact_details = [
        json.loads(item.removeprefix("capture_artifacts:"))
        for item in stored_details
        if isinstance(item, str) and item.startswith("capture_artifacts:")
    ][0]
    assert artifact_details["analysis_path"].startswith(str(seraph_archive))
    assert artifact_details["provider_output_path"].startswith(str(seraph_archive))
    assert Path(artifact_details["analysis_path"]).exists()
    assert Path(artifact_details["provider_output_path"]).exists()

    list_resp = await client.get("/api/observer/screen-artifacts")
    assert list_resp.status_code == 200
    item = list_resp.json()["items"][0]
    assert item["artifacts"]["provider"] == "framekeeper"

    image_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/image")
    assert image_resp.status_code == 200
    assert image_resp.content == image.read_bytes()

    analysis_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/analysis")
    assert analysis_resp.status_code == 200
    analysis = analysis_resp.json()
    assert analysis["provider"] == "framekeeper"
    assert analysis["analysis_provider"] == "deterministic-local"
    assert analysis["image"]["sha256"] == artifact_details["image_sha256"]

    output_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/codex-output")
    assert output_resp.status_code == 200
    assert "Raw screenshot remains in Framekeeper" in output_resp.text


@pytest.mark.asyncio
async def test_framekeeper_manifest_ingest_rejects_path_traversal(async_db, client, tmp_path):
    root = tmp_path / "framekeeper"
    _write_framekeeper_capture(root, capture_id="capture-traversal", image_path="../outside.png")

    resp = await client.post(
        "/api/observer/framekeeper/ingest",
        json={"artifact_root": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ingested"] == 0
    assert payload["rejected"][0]["reason"] == "artifact path must be relative and stay inside capture directory"

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_framekeeper_manifest_ingest_rejects_hash_mismatch(async_db, client, tmp_path):
    root = tmp_path / "framekeeper"
    _write_framekeeper_capture(root, capture_id="capture-hash", image_sha256="0" * 64)

    resp = await client.post(
        "/api/observer/framekeeper/ingest",
        json={"artifact_root": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ingested"] == 0
    assert payload["rejected"][0]["reason"] == "image sha256 mismatch"

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_framekeeper_manifest_ingest_skips_duplicate_capture_id(async_db, client, tmp_path):
    root = tmp_path / "framekeeper"
    _write_framekeeper_capture(root, capture_id="capture-dupe")

    first = await client.post(
        "/api/observer/framekeeper/ingest",
        json={"artifact_root": str(root), "limit": 10},
    )
    second = await client.post(
        "/api/observer/framekeeper/ingest",
        json={"artifact_root": str(root), "limit": 10},
    )

    assert first.status_code == 200
    assert first.json()["ingested"] == 1
    assert second.status_code == 200
    assert second.json()["ingested"] == 0
    assert second.json()["skipped_duplicates"] == 1


def _write_framekeeper_capture(
    root,
    *,
    capture_id: str,
    image_path: str = "screenshot.png",
    image_sha256: str | None = None,
):
    capture_dir = root / "captures" / "2026-06-29" / capture_id
    capture_dir.mkdir(parents=True)
    image = capture_dir / "screenshot.png"
    image.write_bytes(b"framekeeper png bytes")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "capture_id": capture_id,
        "captured_at": "2026-06-29T10:34:56Z",
        "platform": "macos",
        "producer": {"name": "framekeeper", "version": "0.1.0"},
        "reason": "manual",
        "mode": "manual",
        "subject": {
            "app_name": "VS Code",
            "window_title": "framekeeper_source.py",
            "display_id": None,
        },
        "artifacts": {
            "image_path": image_path,
            "image_sha256": image_sha256 or digest,
            "image_media_type": "image/png",
        },
        "policy": {
            "blocked": False,
            "blocklist_version": "builtin-v1",
            "retention_days": 7,
        },
    }
    (capture_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return image
