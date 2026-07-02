import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from src.db.models import ScreenObservation
from src.observer.screenshot_folder_source import ScreenshotFolderAnalysisResult


def test_screenshot_folder_analysis_job_timeout_scales_with_batches(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import _analysis_job_timeout_seconds

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_job_timeout_seconds",
        30,
    )

    assert _analysis_job_timeout_seconds(limit=100, concurrency=1) == 3000
    assert _analysis_job_timeout_seconds(limit=100, concurrency=2) == 1500
    assert _analysis_job_timeout_seconds(limit=1, concurrency=2) == 30


def test_failed_screenshot_analysis_is_retried_after_cooldown():
    from src.observer.screenshot_folder_source import (
        _MAX_ANALYSIS_ATTEMPTS,
        _analysis_candidate_ready,
        _replace_analysis_details,
    )
    from src.observer.screenshot_semantic_analysis import screenshot_analysis_status_detail

    status = screenshot_analysis_status_detail("failed", reason="transient 502", attempts=1)
    details = ["capture_artifacts:{}", status]
    now = datetime.now(timezone.utc)

    assert not _analysis_candidate_ready(details, now=now)
    assert _analysis_candidate_ready(details, now=now + timedelta(minutes=3))

    updated = _replace_analysis_details(details, analysis=None, error_reason="schema validation")
    assert any(f'"attempts":2' in item for item in updated)

    transient = _replace_analysis_details(
        details,
        analysis=None,
        error_reason="502 Bad Gateway",
        consume_attempt=False,
    )
    assert any(f'"attempts":1' in item for item in transient)

    exhausted = [
        "capture_artifacts:{}",
        screenshot_analysis_status_detail("failed", reason="bad output", attempts=_MAX_ANALYSIS_ATTEMPTS),
    ]
    assert not _analysis_candidate_ready(exhausted, now=now + timedelta(minutes=10))


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_job_drains_pending_backlog_with_bounded_concurrency(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis

    calls = []
    events = []
    wait_for_calls = []

    async def fake_accepting_background_work():
        return True

    async def fake_analyze_pending(*, limit, concurrency):
        calls.append({"limit": limit, "concurrency": concurrency})
        return ScreenshotFolderAnalysisResult(scanned=5, analyzed=5, failed=0, skipped=0)

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    async def fake_wait_for(awaitable, *, timeout):
        wait_for_calls.append(timeout)
        return await awaitable

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_accepting_background_work",
        fake_accepting_background_work,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.analyze_pending_screenshot_folder_observations",
        fake_analyze_pending,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.log_scheduler_job_event",
        fake_log_scheduler_job_event,
    )
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.asyncio.wait_for", fake_wait_for)
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_ingest_enabled", True)
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_limit", 999)
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_concurrency",
        99,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_job_timeout_seconds",
        30,
    )

    await run_screenshot_folder_analysis()

    assert calls == [{"limit": 100, "concurrency": 1}]
    assert wait_for_calls == [3000]
    assert events[0]["job_name"] == "screenshot_folder_analysis"
    assert events[0]["outcome"] == "succeeded"
    assert events[0]["details"]["scanned"] == 5
    assert events[0]["details"]["analyzed"] == 5
    assert events[0]["details"]["concurrency"] == 1


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_job_skips_when_vlm_has_no_capacity(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis

    events = []

    async def fake_accepting_background_work():
        return False

    async def fake_analyze_pending(*, limit, concurrency):
        raise AssertionError("analysis should not run when the VLM service is unhealthy")

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_accepting_background_work",
        fake_accepting_background_work,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.analyze_pending_screenshot_folder_observations",
        fake_analyze_pending,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.log_scheduler_job_event",
        fake_log_scheduler_job_event,
    )
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_ingest_enabled", True)

    await run_screenshot_folder_analysis()

    assert events == [
        {
            "job_name": "screenshot_folder_analysis",
            "outcome": "skipped",
            "details": {
                "duration_ms": events[0]["details"]["duration_ms"],
                "reason": "local_vlm_no_background_capacity",
            },
        }
    ]


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_job_does_not_audit_overlap_ticks(monkeypatch):
    import src.scheduler.jobs.screenshot_folder_analysis as job

    events = []

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.log_scheduler_job_event",
        fake_log_scheduler_job_event,
    )

    await job._ANALYSIS_LOCK.acquire()
    try:
        await job.run_screenshot_folder_analysis()
    finally:
        job._ANALYSIS_LOCK.release()

    assert events == []


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_job_reports_degraded_backlog(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis

    events = []

    async def fake_accepting_background_work():
        return True

    async def fake_analyze_pending(*, limit, concurrency):
        return ScreenshotFolderAnalysisResult(scanned=4, analyzed=3, failed=1, skipped=0)

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_accepting_background_work",
        fake_accepting_background_work,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.analyze_pending_screenshot_folder_observations",
        fake_analyze_pending,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.log_scheduler_job_event",
        fake_log_scheduler_job_event,
    )
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_ingest_enabled", True)
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_limit", 1)
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_concurrency", 1)

    await run_screenshot_folder_analysis()

    assert events[0]["outcome"] == "degraded"
    assert events[0]["details"]["scanned"] == 4
    assert events[0]["details"]["analyzed"] == 3
    assert events[0]["details"]["failed"] == 1


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_job_times_out_stuck_analysis(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis

    events = []

    async def fake_accepting_background_work():
        return True

    async def fake_analyze_pending(*, limit, concurrency):
        await asyncio.sleep(1)
        raise AssertionError("stuck analysis should be cancelled by the job timeout")

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_accepting_background_work",
        fake_accepting_background_work,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.analyze_pending_screenshot_folder_observations",
        fake_analyze_pending,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.log_scheduler_job_event",
        fake_log_scheduler_job_event,
    )
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_ingest_enabled", True)
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_concurrency", 2)
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_analysis_job_timeout_seconds",
        0.01,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis._analysis_job_timeout_seconds",
        lambda *, limit, concurrency: 0.01,
    )

    await run_screenshot_folder_analysis()

    assert events[0]["outcome"] == "failed"
    assert events[0]["details"]["error"] == "analysis job timed out"
    assert events[0]["details"]["timeout_seconds"] == 0.01


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_drains_older_pending_rows_behind_newer_succeeded(
    async_db,
    tmp_path,
    monkeypatch,
):
    from src.observer.screenshot_analysis_contract import parse_screenshot_analysis_output
    from src.observer.screenshot_folder_source import analyze_pending_screenshot_folder_observations
    from src.observer.screenshot_semantic_analysis import screenshot_analysis_detail, screenshot_analysis_status_detail

    image = tmp_path / "old-pending.png"
    image.write_bytes(b"old pending screenshot")
    analysis = parse_screenshot_analysis_output(
        {
            "summary": "An older pending screenshot was selected for analysis.",
            "activity_type": "reviewing",
            "confidence": 0.82,
        }
    )
    calls = []

    async def fake_analyze(image_path, artifacts):
        calls.append({"image_path": image_path, "artifacts": dict(artifacts)})
        return analysis

    def capture_artifacts(path, index):
        return "capture_artifacts:" + json.dumps(
            {
                "provider": "screenshot_folder",
                "source": "local_image_directory",
                "image_path": str(path),
                "image_sha256": f"sha-{index}",
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(
            ScreenObservation(
                app_name="Screenshot Folder",
                window_title="old-pending.png",
                activity_type="screen",
                project=None,
                summary="old pending",
                details_json=json.dumps(
                    [
                        capture_artifacts(image, 0),
                        screenshot_analysis_status_detail("pending", reason="queued_for_analysis"),
                    ]
                ),
                blocked=False,
                timestamp=now - timedelta(hours=2),
            )
        )
        succeeded_details = json.dumps(
            [
                capture_artifacts(image, 1),
                screenshot_analysis_detail(analysis),
                screenshot_analysis_status_detail("succeeded"),
            ]
        )
        for index in range(350):
            db.add(
                ScreenObservation(
                    app_name="Screenshot Folder",
                    window_title=f"newer-succeeded-{index}.png",
                    activity_type="screen",
                    project=None,
                    summary="newer succeeded",
                    details_json=succeeded_details,
                    blocked=False,
                    timestamp=now + timedelta(seconds=index),
                )
            )

    monkeypatch.setattr("src.observer.screenshot_folder_source.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.observer.screenshot_folder_source.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_folder_source.analyze_screenshot_image", fake_analyze)

    result = await analyze_pending_screenshot_folder_observations(limit=1)

    assert result.scanned == 1
    assert result.analyzed == 1
    assert calls[0]["image_path"] == image.resolve()
    assert calls[0]["artifacts"]["provider"] == "screenshot_folder"
    async with async_db() as db:
        stored = (
            await db.execute(
                select(ScreenObservation).where(ScreenObservation.window_title == "old-pending.png")
            )
        ).scalar_one()
    assert "screenshot_analysis:" in (stored.details_json or "")
