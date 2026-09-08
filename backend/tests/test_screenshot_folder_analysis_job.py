import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError
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


def test_screenshot_folder_analysis_scheduled_batch_is_limited_to_feeder_window():
    from src.scheduler.jobs.screenshot_folder_analysis import _scheduled_batch_limit

    assert _scheduled_batch_limit(limit=100, concurrency=2, available_slots=2) == 2
    assert _scheduled_batch_limit(limit=1, concurrency=2, available_slots=2) == 1
    assert _scheduled_batch_limit(limit=100, concurrency=1, available_slots=2) == 1
    assert _scheduled_batch_limit(limit=100, concurrency=2, available_slots=0) == 0


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

    transient = _replace_analysis_details(details, analysis=None, error_reason="502 Bad Gateway")
    assert any(f'"attempts":2' in item for item in transient)

    exhausted = [
        "capture_artifacts:{}",
        screenshot_analysis_status_detail("failed", reason="bad output", attempts=_MAX_ANALYSIS_ATTEMPTS),
    ]
    assert not _analysis_candidate_ready(exhausted, now=now + timedelta(minutes=10))


def test_route_denial_is_persisted_as_blocked_and_not_auto_retried():
    from src.model_fabric import NoCompliantModelRouteError
    from src.observer.screenshot_folder_source import (
        _analysis_candidate_ready,
        _is_blocked_analysis_error,
        _replace_analysis_details,
    )
    from src.observer.screenshot_semantic_analysis import (
        ScreenshotSemanticAnalysisError,
        semantic_analysis_status_from_details,
    )

    assert _is_blocked_analysis_error(NoCompliantModelRouteError()) is True
    assert _is_blocked_analysis_error(ScreenshotSemanticAnalysisError("remote_inference_blocked:no_route")) is True
    assert _is_blocked_analysis_error(ScreenshotSemanticAnalysisError("provider_timeout")) is False

    details = _replace_analysis_details(
        ["capture_artifacts:{}"],
        analysis=None,
        error_reason="remote_inference_blocked:no_route",
        status="blocked",
    )
    assert (semantic_analysis_status_from_details(details) or {}).get("status") == "blocked"
    assert _analysis_candidate_ready(details) is False


def test_missing_openrouter_configuration_is_a_blocked_receipt():
    from src.observer.screenshot_folder_source import _replace_analysis_details
    from src.observer.screenshot_semantic_analysis import semantic_analysis_status_from_details

    details = _replace_analysis_details(
        ["capture_artifacts:{}"],
        analysis=None,
        error_reason="remote_inference_blocked:configuration_required",
        status="blocked",
    )
    status = semantic_analysis_status_from_details(details) or {}
    assert status["status"] == "blocked"
    assert status["reason"] == "remote_inference_blocked:configuration_required"


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_selection_retries_database_lock(monkeypatch):
    from src.observer import screenshot_folder_source as source

    for key in source._PERSISTENCE_STATS:
        monkeypatch.setitem(source._PERSISTENCE_STATS, key, 0)
    attempts = 0

    class FakeScalars:
        def all(self):
            return []

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, _statement):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("select", {}, Exception("database is locked"))
            return FakeResult()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(source, "get_session", lambda: FakeSession())
    monkeypatch.setattr(source.asyncio, "sleep", no_sleep)

    result = await source._select_analysis_candidates_with_retry(limit=10)

    assert result == []
    assert attempts == 2
    assert source.screenshot_folder_persistence_status()["selection_db_lock_retries"] == 1
    assert source.screenshot_folder_persistence_status()["db_lock_failures"] == 0


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_persistence_retries_database_lock(monkeypatch):
    from src.observer import screenshot_folder_source as source

    for key in source._PERSISTENCE_STATS:
        monkeypatch.setitem(source._PERSISTENCE_STATS, key, 0)
    attempts = 0

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, _statement):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OperationalError("update", {}, Exception("database is locked"))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(source, "get_session", lambda: FakeSession())
    monkeypatch.setattr(source.asyncio, "sleep", no_sleep)

    await source._persist_analysis_details_with_retry("obs-1", ["capture_artifacts:{}"])

    status = source.screenshot_folder_persistence_status()
    assert attempts == 3
    assert status["persistence_db_lock_retries"] == 2
    assert status["db_lock_failures"] == 0


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_persistence_reports_exhausted_database_lock(monkeypatch):
    from src.observer import screenshot_folder_source as source

    for key in source._PERSISTENCE_STATS:
        monkeypatch.setitem(source._PERSISTENCE_STATS, key, 0)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, _statement):
            raise OperationalError("update", {}, Exception("database is locked"))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(source, "get_session", lambda: FakeSession())
    monkeypatch.setattr(source.asyncio, "sleep", no_sleep)

    with pytest.raises(source.ScreenshotFolderPersistenceError):
        await source._persist_analysis_details_with_retry("obs-1", ["capture_artifacts:{}"])

    status = source.screenshot_folder_persistence_status()
    assert status["persistence_db_lock_retries"] == 3
    assert status["persistence_db_lock_failures"] == 1
    assert status["db_lock_failures"] == 1


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_job_drains_pending_backlog_with_bounded_concurrency(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis

    calls = []
    events = []

    slots = [2, 1, 0]

    async def fake_background_slots():
        return slots.pop(0)

    async def fake_analyze_pending(*, limit, concurrency):
        calls.append({"limit": limit, "concurrency": concurrency})
        return ScreenshotFolderAnalysisResult(scanned=5, analyzed=5, failed=0, skipped=0)

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_background_slots",
        fake_background_slots,
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

    assert calls == [{"limit": 2, "concurrency": 2}, {"limit": 1, "concurrency": 1}]
    assert events[0]["job_name"] == "screenshot_folder_analysis"
    assert events[0]["outcome"] == "succeeded"
    assert events[0]["details"]["scanned"] == 10
    assert events[0]["details"]["analyzed"] == 10
    assert events[0]["details"]["concurrency"] == 2
    assert events[0]["details"]["batch_limit"] == 1
    assert events[0]["details"]["feeder_iterations"] == 2
    assert events[0]["details"]["stopped_reason"] == "remote_inference_no_background_capacity"


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_timeout_releases_scheduler_slot(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis

    events = []
    cancel_seen = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def fake_background_slots():
        return 1

    async def fake_analyze_pending(*, limit, concurrency):
        try:
            await release_cleanup.wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            await release_cleanup.wait()
            raise

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_background_slots",
        fake_background_slots,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.analyze_pending_screenshot_folder_observations",
        fake_analyze_pending,
    )
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.log_scheduler_job_event",
        fake_log_scheduler_job_event,
    )
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis._analysis_job_timeout_seconds", lambda **_: 0.01)
    monkeypatch.setattr("src.scheduler.jobs.screenshot_folder_analysis.settings.screenshot_folder_ingest_enabled", True)

    await asyncio.wait_for(run_screenshot_folder_analysis(), timeout=0.2)
    await asyncio.wait_for(cancel_seen.wait(), timeout=0.2)
    release_cleanup.set()
    await asyncio.sleep(0)

    assert events[0]["job_name"] == "screenshot_folder_analysis"
    assert events[0]["outcome"] == "failed"
    assert events[0]["details"]["error"] == "analysis job timed out"


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_job_skips_when_vlm_has_no_capacity(monkeypatch):
    from src.scheduler.jobs.screenshot_folder_analysis import run_screenshot_folder_analysis

    events = []

    async def fake_background_slots():
        return 0

    async def fake_analyze_pending(*, limit, concurrency):
        raise AssertionError("analysis should not run when the VLM service is unhealthy")

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_background_slots",
        fake_background_slots,
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
                "scanned": 0,
                "analyzed": 0,
                "failed": 0,
                "skipped": 0,
                "concurrency": 2,
                "batch_limit": 0,
                "feeder_iterations": 0,
            "stopped_reason": "remote_inference_no_background_capacity",
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

    async def fake_background_slots():
        return 1

    async def fake_analyze_pending(*, limit, concurrency):
        return ScreenshotFolderAnalysisResult(scanned=4, analyzed=3, failed=1, skipped=0)

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_background_slots",
        fake_background_slots,
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

    async def fake_background_slots():
        return 1

    async def fake_analyze_pending(*, limit, concurrency):
        await asyncio.sleep(1)
        raise AssertionError("stuck analysis should be cancelled by the job timeout")

    async def fake_log_scheduler_job_event(*, job_name, outcome, details):
        events.append({"job_name": job_name, "outcome": outcome, "details": details})

    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_folder_analysis.screenshot_semantic_analysis_background_slots",
        fake_background_slots,
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
                        _capture_artifacts(image, 0),
                        screenshot_analysis_status_detail("pending", reason="queued_for_analysis"),
                    ]
                ),
                blocked=False,
                timestamp=now - timedelta(hours=2),
            )
        )
        succeeded_details = json.dumps(
            [
                _capture_artifacts(image, 1),
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
    monkeypatch.setattr("src.observer.screenshot_folder_source.screenshot_semantic_analysis_enabled", lambda: True)
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


@pytest.mark.asyncio
async def test_screenshot_folder_analysis_counts_persistence_lock_as_failed(
    async_db,
    tmp_path,
    monkeypatch,
):
    from src.observer.screenshot_analysis_contract import parse_screenshot_analysis_output
    from src.observer.screenshot_folder_source import (
        ScreenshotFolderPersistenceError,
        _persist_analysis_details_with_retry,
        analyze_pending_screenshot_folder_observations,
    )
    from src.observer.screenshot_semantic_analysis import screenshot_analysis_status_detail

    image = tmp_path / "pending-lock.png"
    image.write_bytes(b"pending screenshot")
    analysis = parse_screenshot_analysis_output(
        {
            "summary": "A pending screenshot was analyzed but persistence failed.",
            "activity_type": "reviewing",
            "confidence": 0.82,
        }
    )

    async def fake_analyze(_image_path, _artifacts):
        return analysis

    persist_locked = True

    async def locked_persist(_observation_id, _details):
        if persist_locked:
            raise ScreenshotFolderPersistenceError("database is locked while saving screenshot analysis")
        await _persist_analysis_details_with_retry(_observation_id, _details)

    async with async_db() as db:
        db.add(
            ScreenObservation(
                app_name="Screenshot Folder",
                window_title="pending-lock.png",
                activity_type="screen",
                project=None,
                summary="pending lock",
                details_json=json.dumps(
                    [
                        _capture_artifacts(image, 0),
                        screenshot_analysis_status_detail("pending", reason="queued_for_analysis"),
                    ]
                ),
                blocked=False,
                timestamp=datetime.now(timezone.utc),
            )
        )

    monkeypatch.setattr("src.observer.screenshot_folder_source.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.observer.screenshot_folder_source.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_folder_source.screenshot_semantic_analysis_enabled", lambda: True)
    monkeypatch.setattr("src.observer.screenshot_folder_source.analyze_screenshot_image", fake_analyze)
    monkeypatch.setattr("src.observer.screenshot_folder_source._persist_analysis_details_with_retry", locked_persist)

    result = await analyze_pending_screenshot_folder_observations(limit=1)

    assert result.scanned == 1
    assert result.analyzed == 0
    assert result.failed == 1
    async with async_db() as db:
        stored = (
            await db.execute(
                select(ScreenObservation).where(ScreenObservation.window_title == "pending-lock.png")
            )
        ).scalar_one()
    assert "screenshot_analysis:" not in (stored.details_json or "")

    persist_locked = False
    second = await analyze_pending_screenshot_folder_observations(limit=1)

    assert second.scanned == 1
    assert second.analyzed == 1
    assert second.failed == 0
    async with async_db() as db:
        drained = (
            await db.execute(
                select(ScreenObservation).where(ScreenObservation.window_title == "pending-lock.png")
            )
        ).scalar_one()
    assert "screenshot_analysis:" in (drained.details_json or "")


def _capture_artifacts(path, index):
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
