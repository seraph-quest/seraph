"""#1112: the guardian opportunity and screenshot folder analysis toggles.

No provider is contacted: the opportunity assessment executor and the scheduled
job bodies are replaced with recorders that fail if a model call is attempted.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlmodel import select

from config.settings import settings
from src.db.models import GuardianOpportunity
from src.guardian import opportunity_runtime as runtime
from src.scheduler import background_inference
from src.scheduler.engine import _async_job_wrapper


@pytest.fixture
def toggles(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    background_inference._last_status.clear()
    runtime._executions.clear()
    yield
    background_inference._last_status.clear()
    runtime._executions.clear()


def _status(job_id):
    return {job["job_id"]: job for job in background_inference.background_inference_status()["jobs"]}[job_id]


async def _seed(*, deadline):
    from src.db.engine import get_session
    now = datetime.now(timezone.utc)
    row = GuardianOpportunity(owner_principal_id="operator:owner", original_root_id="root", goal_id="goal",
        goal_revision=1, policy_revision=1, watch_id="watch", watch_revision=1, source_packet_id="packet",
        source_digest="a" * 64, source_token_json="{}", dedupe_key=uuid4().hex,
        expires_at=now + timedelta(days=1), assessment_deadline_at=deadline)
    async with get_session() as db:
        db.add(row)
    return row.id


async def _rows():
    from src.db.engine import get_session
    async with get_session() as db:
        return {row.id: row for row in (await db.execute(select(GuardianOpportunity))).scalars()}


def test_both_jobs_are_registered_default_on_with_event_driven_estimates(toggles):
    for job_id, scope in (("guardian_opportunity_assessment", "llm_only"), ("screenshot_folder_analysis", "job")):
        job = _status(job_id)
        assert job["enabled"] is True and job["scope"] == scope
        # Event-driven: the basis text, not a cadence, is the estimate.
        assert job["approx_calls_per_day"] is None and job["estimate_basis"]
    assert "16 per tick" in _status("guardian_opportunity_assessment")["estimate_basis"]
    assert "vision" in _status("screenshot_folder_analysis")["estimate_basis"]


@pytest.mark.asyncio
async def test_disabled_opportunity_assessment_makes_no_model_call_and_keeps_candidates_visible(async_db, toggles):
    background_inference.write_background_inference_settings({"guardian_opportunity_assessment": False})
    now = datetime.now(timezone.utc)
    live = await _seed(deadline=now + timedelta(hours=1))
    expired = await _seed(deadline=now - timedelta(minutes=1))
    executor = AsyncMock(side_effect=AssertionError("assessment model call must not start"))
    with patch.object(runtime, "assert_coalesced_predecessor_closed", AsyncMock(return_value=None)), \
            patch.object(runtime, "execute_assessment", executor):
        tick = await runtime.run_opportunity_tick()
        again = await runtime.run_opportunity_tick()
    assert tick["started"] == again["started"] == 0
    executor.assert_not_called()
    rows = await _rows()
    # The live candidate stays queued and visibly says why; the expired one is
    # closed as expired, never dropped silently.
    assert (rows[live].status, rows[live].reason_code) == ("queued", "disabled_by_operator")
    assert (rows[expired].status, rows[expired].reason_code) == ("blocked", "assessment_deadline_expired")
    # A repeated paused tick does not churn the visible row.
    assert rows[live].revision == 2
    assert _status("guardian_opportunity_assessment")["last_skip"]["reason"] == "disabled_by_operator"

    # Switching it back on resumes the same candidate exactly once.
    background_inference.write_background_inference_settings({"guardian_opportunity_assessment": True})
    resumed = AsyncMock(return_value=None)
    with patch.object(runtime, "assert_coalesced_predecessor_closed", AsyncMock(return_value=None)), \
            patch.object(runtime, "execute_assessment", resumed):
        tick = await runtime.run_opportunity_tick()
        await asyncio.gather(*list(runtime._executions.values()), return_exceptions=True)
    assert tick["started"] == 1
    resumed.assert_awaited_once_with(live)


@pytest.mark.asyncio
async def test_unreadable_toggle_file_pauses_opportunity_assessment(async_db, toggles):
    background_inference.background_inference_settings_path().write_text("{broken", encoding="utf-8")
    live = await _seed(deadline=datetime.now(timezone.utc) + timedelta(hours=1))
    executor = AsyncMock(side_effect=AssertionError("assessment model call must not start"))
    with patch.object(runtime, "assert_coalesced_predecessor_closed", AsyncMock(return_value=None)), \
            patch.object(runtime, "execute_assessment", executor):
        assert (await runtime.run_opportunity_tick())["started"] == 0
    executor.assert_not_called()
    row = (await _rows())[live]
    assert (row.status, row.reason_code) == ("queued", background_inference.SETTINGS_UNREADABLE)
    assert _status("guardian_opportunity_assessment")["last_skip"]["reason"] == background_inference.SETTINGS_UNREADABLE


@pytest.mark.parametrize("unreadable", [False, True])
def test_disabled_screenshot_folder_analysis_makes_no_vision_call(toggles, unreadable):
    if unreadable:
        background_inference.background_inference_settings_path().write_text("{broken", encoding="utf-8")
    else:
        background_inference.write_background_inference_settings({"screenshot_folder_analysis": False})
    calls = []

    async def analysis():
        # Stands in for run_screenshot_folder_analysis: any run reaches the vision route.
        calls.append("vision")

    async def run():
        audit = AsyncMock()
        with patch("src.audit.runtime.log_scheduler_job_event", audit):
            await _async_job_wrapper(analysis, asyncio.get_running_loop(), job_id="screenshot_folder_analysis",
                                     allow_model_inference=True)()
        return audit

    audit = asyncio.run(run())
    reason = background_inference.SETTINGS_UNREADABLE if unreadable else "disabled_by_operator"
    assert calls == []
    audit.assert_awaited_once_with(job_name="screenshot_folder_analysis", outcome="skipped", details={"reason": reason})
    assert _status("screenshot_folder_analysis")["last_skip"]["reason"] == reason
