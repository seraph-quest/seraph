"""Tests for preserved screen capture artifact inspection endpoints."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from src.db.models import ScreenObservation
from src.observer.screenshot_analysis_contract import ScreenshotAnalysis


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
async def test_screen_artifacts_ignore_named_external_provider(async_db, client, tmp_path, monkeypatch):
    monkeypatch.setattr("src.api.observer.settings.screen_capture_archive_dir", str(tmp_path))
    image_path = tmp_path / "capture.png"
    image_path.write_bytes(b"png bytes")
    artifacts = {
        "id": "external-recorder-artifact",
        "provider": "external_recorder",
        "screenshot_folder": str(tmp_path),
        "image_path": str(image_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    async with async_db() as db:
        observation = ScreenObservation(
            app_name="External Recorder",
            window_title="capture.png",
            activity_type="screen",
            summary="Producer-specific artifact",
            details_json=json.dumps(["capture_artifacts:" + json.dumps(artifacts)]),
        )
        db.add(observation)

    list_resp = await client.get("/api/observer/screen-artifacts")
    assert list_resp.status_code == 200
    assert list_resp.json()["items"] == []

    image_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/image")
    assert image_resp.status_code == 404


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
async def test_screenshot_folder_scan_persists_observation_and_serves_image(async_db, client, tmp_path):
    root = tmp_path / "screenshots"
    image = _write_screenshot(root, name="capture-valid.png")

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["screenshot_folder"] == str(root.resolve())
    assert "artifact_root" not in payload
    assert payload["scanned"] == 1
    assert payload["ingested"] == 1
    assert payload["rejected"] == []

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    assert observation.app_name == "Screenshot Folder"
    assert observation.summary == "Screenshot image added from folder: capture-valid.png (png, 9 B)."
    stored_details = json.loads(observation.details_json or "[]")
    image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
    assert f"screenshot_folder_image_sha256:{image_sha256}" in stored_details
    artifact_details = [
        json.loads(item.removeprefix("capture_artifacts:"))
        for item in stored_details
        if isinstance(item, str) and item.startswith("capture_artifacts:")
    ][0]
    assert artifact_details["provider"] == "screenshot_folder"
    assert artifact_details["source"] == "local_image_directory"
    assert artifact_details["screenshot_folder"] == str(root.resolve())
    assert "artifact_root" not in artifact_details
    assert artifact_details["image_path"] == str(image.resolve())
    assert artifact_details["image_sha256"] == image_sha256
    assert artifact_details["file_format"] == "png"
    assert artifact_details["image_bytes"] == len(image.read_bytes())
    assert artifact_details["width"] is None
    assert artifact_details["height"] is None
    assert "analysis_path" not in artifact_details
    assert "provider_output_path" not in artifact_details

    list_resp = await client.get("/api/observer/screen-artifacts")
    assert list_resp.status_code == 200
    item = list_resp.json()["items"][0]
    assert item["artifacts"]["provider"] == "screenshot_folder"
    assert item["artifacts"]["image_url"].endswith(f"/{observation.id}/image")
    assert item["artifacts"]["analysis_url"].endswith(f"/{observation.id}/analysis")
    assert "codex_output_url" not in item["artifacts"]
    assert "provider_output_url" not in item["artifacts"]

    image_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/image")
    assert image_resp.status_code == 200
    assert image_resp.content == image.read_bytes()

    analysis_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/analysis")
    assert analysis_resp.status_code == 200
    analysis = analysis_resp.json()
    assert analysis["provider"] == "screenshot_folder"
    assert analysis["analysis"]["analysis_owner"] == "seraph"
    assert analysis["analysis"]["source"] == "local_screenshot_folder"
    assert analysis["analysis"]["image_sha256"] == image_sha256
    assert analysis["analysis"]["image_bytes"] == len(image.read_bytes())
    assert analysis["analysis"]["file_format"] == "png"
    assert analysis["analysis"]["report_ready"] is True
    assert analysis["image_sha256"] == image_sha256

    output_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/codex-output")
    assert output_resp.status_code == 200
    assert "screenshot folder source only provided the image file" in output_resp.text


@pytest.mark.asyncio
async def test_screenshot_folder_scan_persists_local_vlm_semantic_analysis(
    async_db, client, tmp_path, monkeypatch
):
    root = tmp_path / "screenshots"
    image = _write_screenshot(root, name="capture-semantic.png", data=_sample_png())
    image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
    calls = []

    async def fake_analyze_screenshot_image(image_path, artifacts):
        calls.append((image_path, artifacts))
        return ScreenshotAnalysis.model_validate(
            {
                "schema_version": "seraph.screenshot_analysis.v1",
                "prompt_version": "seraph.screenshot_analysis.prompt.v1",
                "summary": "The user is implementing screenshot ingestion in Seraph.",
                "detailed_observations": ["A code editor and tests are visible."],
                "activity_type": "coding",
                "project": "seraph",
                "applications": ["editor"],
                "visible_artifacts": ["test_observer_screen_artifacts.py"],
                "key_visible_text": ["screenshot-folder/scan"],
                "user_intent": "Wire screenshot folder images into analysis.",
                "goal_alignment": {
                    "status": "aligned",
                    "goal_refs": ["screenshot intelligence loop"],
                    "evidence": ["The visible file concerns screenshot ingestion."],
                    "needle_movement": "pushed",
                },
                "confidence": 0.86,
                "sensitive_content_seen": False,
                "privacy_notes": [],
                "report_tags": ["screenshots", "seraph"],
            }
        )

    monkeypatch.setattr(
        "src.observer.screenshot_folder_source.analyze_screenshot_image",
        fake_analyze_screenshot_image,
    )

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    assert resp.json()["ingested"] == 1
    assert calls
    assert calls[0][0] == image.resolve()
    assert calls[0][1]["image_sha256"] == image_sha256

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    stored_details = json.loads(observation.details_json or "[]")
    semantic_details = [
        json.loads(item.removeprefix("screenshot_analysis:"))
        for item in stored_details
        if isinstance(item, str) and item.startswith("screenshot_analysis:")
    ]
    assert semantic_details[0]["summary"] == "The user is implementing screenshot ingestion in Seraph."
    assert semantic_details[0]["goal_alignment"]["needle_movement"] == "pushed"

    analysis_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/analysis")
    assert analysis_resp.status_code == 200
    analysis = analysis_resp.json()["analysis"]
    assert analysis["semantic_status"] == "ready"
    assert analysis["semantic_analysis"]["project"] == "seraph"
    assert analysis["semantic_analysis"]["activity_type"] == "coding"
    assert analysis["semantic_analysis"]["goal_alignment"]["status"] == "aligned"
    assert analysis["semantic_error"] is None


@pytest.mark.asyncio
async def test_screenshot_folder_scan_keeps_metadata_when_local_vlm_fails(
    async_db, client, tmp_path, monkeypatch
):
    from src.observer.screenshot_semantic_analysis import ScreenshotSemanticAnalysisError

    root = tmp_path / "screenshots"
    _write_screenshot(root, name="capture-failure.png")

    async def failing_analyze_screenshot_image(_image_path, _artifacts):
        raise ScreenshotSemanticAnalysisError("provider unavailable")

    monkeypatch.setattr(
        "src.observer.screenshot_folder_source.analyze_screenshot_image",
        failing_analyze_screenshot_image,
    )

    first = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )
    second = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert first.status_code == 200
    assert first.json()["ingested"] == 1
    assert first.json()["rejected"] == []
    assert second.status_code == 200
    assert second.json()["ingested"] == 0
    assert second.json()["skipped_duplicates"] == 1

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    stored_details = json.loads(observation.details_json or "[]")
    error_details = [
        json.loads(item.removeprefix("screenshot_analysis_error:"))
        for item in stored_details
        if isinstance(item, str) and item.startswith("screenshot_analysis_error:")
    ]
    assert error_details[0]["reason"] == "provider unavailable"

    analysis_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/analysis")
    assert analysis_resp.status_code == 200
    analysis = analysis_resp.json()["analysis"]
    assert analysis["semantic_status"] == "failed"
    assert analysis["semantic_error"]["reason"] == "provider unavailable"
    assert analysis["semantic_analysis"] is None


@pytest.mark.asyncio
async def test_screenshot_folder_scan_ignores_non_images(async_db, client, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    (root / "notes.txt").write_text("not a screenshot", encoding="utf-8")

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scanned"] == 0
    assert payload["ingested"] == 0
    assert payload["rejected"] == []
    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_screenshot_folder_scan_ignores_temporary_write_files(async_db, client, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    (root / "capture.png.tmp").write_bytes(b"in-progress screenshot")

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scanned"] == 0
    assert payload["ingested"] == 0
    assert payload["rejected"] == []
    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_screenshot_folder_scan_rejects_symlink_escape(async_db, client, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside screenshot")
    link = root / "linked-outside.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this filesystem")

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scanned"] == 1
    assert payload["ingested"] == 0
    assert payload["skipped_duplicates"] == 0
    assert payload["rejected"] == [
        {"image_path": str(outside.resolve()), "reason": "image is outside screenshot folder root"}
    ]
    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_screenshot_folder_scan_rejects_broad_workspace_root(client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capture.png").write_bytes(b"not a dedicated screenshot folder")
    monkeypatch.setattr("src.observer.screenshot_folder_source.settings.workspace_dir", str(workspace))

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(workspace), "limit": 10},
    )

    assert resp.status_code == 400
    assert "dedicated image directory" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_screenshot_folder_scan_rejects_legacy_artifact_root_request_key(client, tmp_path):
    root = tmp_path / "screenshots"
    root.mkdir()

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"artifact_root": str(root), "limit": 10},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_screenshot_folder_image_analysis_extracts_png_dimensions(async_db, client, tmp_path):
    root = tmp_path / "screenshots"
    image = _write_screenshot(root, name="capture-real.png", data=_sample_png())

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    analysis_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/analysis")
    assert analysis_resp.status_code == 200
    analysis = analysis_resp.json()["analysis"]
    assert analysis["file_format"] == "png"
    assert analysis["width"] == 1
    assert analysis["height"] == 1
    assert analysis["image_path"] == str(image.resolve())
    stored_details = json.loads(observation.details_json or "[]")
    artifact_details = [
        json.loads(item.removeprefix("capture_artifacts:"))
        for item in stored_details
        if isinstance(item, str) and item.startswith("capture_artifacts:")
    ][0]
    assert artifact_details["file_format"] == "png"
    assert artifact_details["width"] == 1
    assert artifact_details["height"] == 1
    assert observation.summary == "Screenshot image added from folder: capture-real.png (png, 1x1, 67 B)."


@pytest.mark.asyncio
async def test_screenshot_folder_jpeg_artifact_uses_jpeg_media_type(async_db, client, tmp_path):
    root = tmp_path / "screenshots"
    _write_screenshot(root, name="capture.jpg")

    resp = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert resp.status_code == 200
    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    image_resp = await client.get(f"/api/observer/screen-artifacts/{observation.id}/image")
    assert image_resp.status_code == 200
    assert image_resp.headers["content-type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_screenshot_folder_scan_skips_duplicate_hash(async_db, client, tmp_path):
    root = tmp_path / "screenshots"
    _write_screenshot(root, name="capture-dupe.png")

    first = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )
    second = await client.post(
        "/api/observer/screenshot-folder/scan",
        json={"screenshot_folder": str(root), "limit": 10},
    )

    assert first.status_code == 200
    assert first.json()["ingested"] == 1
    assert second.status_code == 200
    assert second.json()["ingested"] == 0
    assert second.json()["skipped_duplicates"] == 1


def _write_screenshot(root: Path, *, name: str, data: bytes = b"png bytes") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    image = root / name
    image.write_bytes(data)
    return image


def _sample_png() -> bytes:
    return bytes(
        [
            0x89,
            0x50,
            0x4E,
            0x47,
            0x0D,
            0x0A,
            0x1A,
            0x0A,
            0x00,
            0x00,
            0x00,
            0x0D,
            0x49,
            0x48,
            0x44,
            0x52,
            0x00,
            0x00,
            0x00,
            0x01,
            0x00,
            0x00,
            0x00,
            0x01,
            0x08,
            0x06,
            0x00,
            0x00,
            0x00,
            0x1F,
            0x15,
            0xC4,
            0x89,
            0x00,
            0x00,
            0x00,
            0x0A,
            0x49,
            0x44,
            0x41,
            0x54,
            0x78,
            0x9C,
            0x63,
            0x00,
            0x01,
            0x00,
            0x00,
            0x05,
            0x00,
            0x01,
            0x0D,
            0x0A,
            0x2D,
            0xB4,
            0x00,
            0x00,
            0x00,
            0x00,
            0x49,
            0x45,
            0x4E,
            0x44,
            0xAE,
            0x42,
            0x60,
            0x82,
        ]
    )
