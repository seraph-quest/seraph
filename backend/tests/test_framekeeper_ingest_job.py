import hashlib
import json
from pathlib import Path

import pytest
from sqlmodel import select

from src.db.models import ScreenObservation


@pytest.mark.asyncio
async def test_framekeeper_ingest_job_reads_images_from_configured_folder(async_db, tmp_path, monkeypatch):
    from src.scheduler.jobs.framekeeper_ingest import run_framekeeper_image_ingest

    root = tmp_path / "framekeeper"
    image = _write_screenshot(root, "job-capture.png")
    monkeypatch.setenv("SERAPH_FRAMEKEEPER_ARTIFACT_ROOT", str(root))
    monkeypatch.setattr("src.scheduler.jobs.framekeeper_ingest.settings.framekeeper_ingest_enabled", True)
    monkeypatch.setattr("src.scheduler.jobs.framekeeper_ingest.settings.framekeeper_ingest_limit", 20)

    await run_framekeeper_image_ingest()

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        observation = result.scalar_one()

    image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
    details = json.loads(observation.details_json or "[]")
    assert observation.app_name == "Framekeeper"
    assert observation.window_title == "job-capture.png"
    assert f"framekeeper_image_sha256:{image_sha256}" in details
    artifact_payloads = [
        json.loads(item.removeprefix("capture_artifacts:"))
        for item in details
        if isinstance(item, str) and item.startswith("capture_artifacts:")
    ]
    assert artifact_payloads[0]["provider"] == "framekeeper"
    assert artifact_payloads[0]["image_path"] == str(image.resolve())
    assert "manifest" not in json.dumps(artifact_payloads[0]).lower()


@pytest.mark.asyncio
async def test_framekeeper_ingest_job_respects_disabled_setting(async_db, tmp_path, monkeypatch):
    from src.scheduler.jobs.framekeeper_ingest import run_framekeeper_image_ingest

    root = tmp_path / "framekeeper"
    _write_screenshot(root, "disabled.png")
    monkeypatch.setenv("SERAPH_FRAMEKEEPER_ARTIFACT_ROOT", str(root))
    monkeypatch.setattr("src.scheduler.jobs.framekeeper_ingest.settings.framekeeper_ingest_enabled", False)

    await run_framekeeper_image_ingest()

    async with async_db() as db:
        result = await db.execute(select(ScreenObservation))
        assert result.scalar_one_or_none() is None


def _write_screenshot(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    image = root / name
    image.write_bytes(b"framekeeper scheduled screenshot")
    return image
