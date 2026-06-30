"""Full-loop tests for screenshot-folder intelligence and reports."""

from __future__ import annotations

import os
import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from config.settings import settings


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_digest_report_and_status_loop(
    async_db,
    client,
    tmp_path,
    monkeypatch,
):
    from src.db.models import Goal, MemoryEpisode, ScreenObservation
    from src.observer.screenshot_analysis_contract import parse_screenshot_analysis_output
    from src.observer.screenshot_folder_source import scan_screenshot_folder
    from src.scheduler.jobs.end_of_day_goal_report import build_end_of_day_goal_report
    from src.scheduler.jobs.screenshot_observation_digest import build_screenshot_observation_digest
    from sqlmodel import select

    screenshot_root = tmp_path / "screenshots"
    screenshot_root.mkdir()
    image_bytes = b"fake png bytes"
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    image = screenshot_root / "seraph-loop.png"
    duplicate_image = screenshot_root / "seraph-loop-copy.png"
    image.write_bytes(image_bytes)
    duplicate_image.write_bytes(image_bytes)
    captured_at = datetime(2026, 6, 30, 10, 5, tzinfo=timezone.utc)
    os.utime(image, (captured_at.timestamp(), captured_at.timestamp()))
    os.utime(duplicate_image, (captured_at.timestamp(), captured_at.timestamp()))
    analysis = parse_screenshot_analysis_output(
        {
            "schema_version": "seraph.screenshot_analysis.v1",
            "prompt_version": "seraph.screenshot_analysis.prompt.v1",
            "summary": "The user is implementing Seraph screenshot intelligence loop tests.",
            "detailed_observations": [
                "A test covers screenshot ingestion, digest creation, and reporting."
            ],
            "activity_type": "coding",
            "project": "seraph",
            "applications": ["editor"],
            "visible_artifacts": ["test_screenshot_intelligence_loop.py"],
            "key_visible_text": ["screenshot intelligence loop"],
            "user_intent": "Verify the complete Seraph screenshot analysis path.",
            "goal_alignment": {
                "status": "aligned",
                "goal_refs": ["Seraph screenshot intelligence loop"],
                "evidence": ["The visible work is the screenshot loop test."],
                "needle_movement": "pushed",
            },
            "confidence": 0.9,
            "sensitive_content_seen": False,
            "privacy_notes": [],
            "report_tags": ["screenshots", "daily-report"],
        }
    )

    analyzer_calls = []

    async def fake_analyze_screenshot_image(_image_path, _artifacts):
        analyzer_calls.append((_image_path, dict(_artifacts)))
        return analysis

    monkeypatch.setattr(
        "src.observer.screenshot_folder_source.analyze_screenshot_image",
        fake_analyze_screenshot_image,
    )
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(screenshot_root))
    monkeypatch.setattr("src.api.settings.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.api.settings.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.api.settings.settings.local_vlm_model", "gemma-test")
    async with async_db() as db:
        db.add(
            Goal(
                id="goal-loop",
                path="/",
                level="daily",
                title="Ship Seraph screenshot intelligence loop",
                domain="productivity",
                status="active",
                sort_order=0,
            )
        )

    scan_result = await scan_screenshot_folder(screenshot_root, limit=10)
    duplicate_scan_result = await scan_screenshot_folder(screenshot_root, limit=10)
    digest_result = await build_screenshot_observation_digest(
        window_start=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 6, 30, 10, 30, tzinfo=timezone.utc),
    )
    with monkeypatch.context() as report_patch:
        report_patch.setattr(settings, "user_timezone", "UTC")
        report_patch.setattr(settings, "end_of_day_report_llm_enabled", False)
        report = await build_end_of_day_goal_report(date(2026, 6, 30))

    status = (await client.get("/api/settings/artifact-storage")).json()
    async with async_db() as db:
        observations = list((await db.execute(select(ScreenObservation))).scalars().all())
        episodes = list((await db.execute(select(MemoryEpisode))).scalars().all())

    assert scan_result.ingested == 1
    assert scan_result.skipped_duplicates == 1
    assert duplicate_scan_result.ingested == 0
    assert duplicate_scan_result.skipped_duplicates == 2
    assert len(analyzer_calls) == 1
    analyzed_path, analyzed_artifacts = analyzer_calls[0]
    assert analyzed_path in {image.resolve(), duplicate_image.resolve()}
    assert analyzed_artifacts["provider"] == "screenshot_folder"
    assert analyzed_artifacts["source"] == "local_image_directory"
    assert analyzed_artifacts["created_at"] == captured_at.isoformat()
    assert analyzed_artifacts["image_sha256"] == image_sha256
    assert "manifest" not in analyzed_artifacts
    assert "service" not in analyzed_artifacts
    assert len(observations) == 1
    assert observations[0].timestamp.replace(tzinfo=timezone.utc) == captured_at
    details = json.loads(observations[0].details_json or "[]")
    assert f"screenshot_folder_image_sha256:{image_sha256}" in details
    assert any(str(item).startswith("capture_artifacts:") for item in details)
    assert any(str(item).startswith("screenshot_analysis:") for item in details)
    assert any(str(item).startswith("screenshot_analysis_status:") for item in details)
    assert digest_result.status == "created"
    assert digest_result.observation_count == 1
    digest_episode = next(
        episode for episode in episodes if episode.source_tool_name == "screenshot_observation_digest"
    )
    digest_metadata = json.loads(digest_episode.metadata_json or "{}")
    assert digest_metadata["observation_ids"] == [observations[0].id]
    assert digest_metadata["observation_count"] == 1
    assert "Evidence observation IDs:" in digest_episode.content
    assert observations[0].id in digest_episode.content
    assert "screenshot intelligence loop" in digest_episode.content
    assert str(image.resolve()) not in digest_episode.content
    assert image_sha256 not in digest_episode.content
    assert report["screenshot_digests"]["count"] == 1
    assert report["screenshot_digests"]["observation_ids"] == ["[redacted]"]
    assert observations[0].id not in report["screenshot_digests"]["redacted_text"]
    assert "[redacted]" in report["screenshot_digests"]["redacted_text"]
    assert "screenshot intelligence loop" in report["screenshot_digests"]["redacted_text"]
    assert str(image.resolve()) not in report["screenshot_digests"]["redacted_text"]
    assert image_sha256 not in report["screenshot_digests"]["redacted_text"]
    assert report["goal_alignment"][0]["status"] == "aligned"
    assert report["goal_alignment"][0]["needle_movement"] == "pushed"
    assert report["goal_alignment"][0]["source_observation_ids"] == ["[redacted]"]
    assert "- Pushed-the-needle goals: 1" in report["body"]
    assert status["screenshot_folder"]["analysis"]["observation_count"] == 1
    assert status["screenshot_folder"]["analysis"]["analysis_status"]["succeeded"] == 1
    assert status["screenshot_folder"]["analysis"]["analysis_failures"] == 0
    assert status["screenshot_folder"]["analysis"]["analysis_backlog"] == 0
    assert status["screenshot_folder"]["analysis"]["digest_count"] == 1
