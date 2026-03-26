from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from smolagents import Tool

from config.settings import settings
from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import get_current_session_id
from src.observer.intervention_policy import DeliveryDecision, InterventionAction, InterventionDecision
from src.scheduler.scheduled_jobs import execute_scheduled_job, scheduled_job_repository
from src.db.models import ScheduledJob, Session


class DummyWorkflowTool(Tool):
    name = "workflow_brief_sync"
    description = "Dummy workflow"
    inputs = {"topic": {"type": "string", "description": "Topic"}}
    output_type = "string"

    def __init__(self):
        super().__init__()
        self.seen_session_id = None

    def forward(self, topic: str) -> str:
        self.seen_session_id = get_current_session_id()
        return f"done:{topic}"


@pytest.mark.asyncio
async def test_create_scheduled_job_falls_back_invalid_timezone(async_db):
    with patch.object(settings, "user_timezone", "UTC"):
        job = await scheduled_job_repository.create_job(
            name="Morning check",
            cron="0 9 * * *",
            timezone_name="Mars/Phobos",
            target_type="message",
            content="Stand up and review priorities.",
            intervention_type="advisory",
            urgency=3,
            workflow_name="",
            workflow_args_json="",
            session_id="s1",
            created_by_session_id="s1",
        )

    assert job["trigger_spec"]["timezone"] == "UTC"
    assert job["action_type"] == "deliver_message"


@pytest.mark.asyncio
async def test_execute_scheduled_job_delivers_message(async_db):
    job = await scheduled_job_repository.create_job(
        name="Morning check",
        cron="0 9 * * *",
        timezone_name="UTC",
        target_type="message",
        content="Stand up and review priorities.",
        intervention_type="advisory",
        urgency=3,
        workflow_name="",
        workflow_args_json="",
        session_id="s1",
        created_by_session_id="s1",
    )

    with patch(
        "src.scheduler.scheduled_jobs.deliver_or_queue",
        AsyncMock(
            return_value=InterventionDecision(
                action=InterventionAction.act,
                reason="scheduled",
                delivery_decision=DeliveryDecision.deliver,
                should_cost_budget=False,
            )
        ),
    ) as mock_deliver:
        await execute_scheduled_job(job["id"])

    stored = await scheduled_job_repository.get_job(job["id"])
    assert stored is not None
    assert stored["last_outcome"] == "delivered"
    assert stored["last_error"] is None
    assert stored["last_run_at"] is not None
    assert mock_deliver.await_args.kwargs["is_scheduled"] is True
    assert mock_deliver.await_args.kwargs["session_id"] == "s1"


@pytest.mark.asyncio
async def test_execute_scheduled_job_runs_wrapped_workflow_with_session_context(async_db):
    workflow = type("Workflow", (), {"name": "brief-sync", "tool_name": "workflow_brief_sync", "enabled": True})()
    tool = DummyWorkflowTool()

    with patch("src.scheduler.scheduled_jobs.workflow_manager.get_workflow", return_value=workflow):
        job = await scheduled_job_repository.create_job(
            name="Workflow check",
            cron="0 9 * * *",
            timezone_name="UTC",
            target_type="workflow",
            content="",
            intervention_type="advisory",
            urgency=3,
            workflow_name="brief-sync",
            workflow_args_json='{"topic":"guardian"}',
            session_id="s1",
            created_by_session_id="s1",
        )
        with patch("src.agent.factory.get_tools", return_value=[tool]):
            await execute_scheduled_job(job["id"])

    stored = await scheduled_job_repository.get_job(job["id"])
    assert stored is not None
    assert stored["last_outcome"] == "succeeded"
    assert tool.seen_session_id == "s1"


@pytest.mark.asyncio
async def test_execute_scheduled_job_records_approval_required(async_db):
    workflow = type("Workflow", (), {"name": "brief-sync", "tool_name": "workflow_brief_sync", "enabled": True})()
    failing_tool = MagicMock(name="workflow_brief_sync")
    failing_tool.name = "workflow_brief_sync"
    failing_tool.side_effect = ApprovalRequired(
        approval_id="approval-123",
        session_id="s1",
        tool_name="workflow_brief_sync",
        risk_level="high",
        summary="needs approval",
    )

    with patch("src.scheduler.scheduled_jobs.workflow_manager.get_workflow", return_value=workflow):
        job = await scheduled_job_repository.create_job(
            name="Workflow check",
            cron="0 9 * * *",
            timezone_name="UTC",
            target_type="workflow",
            content="",
            intervention_type="advisory",
            urgency=3,
            workflow_name="brief-sync",
            workflow_args_json="{}",
            session_id="s1",
            created_by_session_id="s1",
        )
        with patch("src.agent.factory.get_tools", return_value=[failing_tool]):
            await execute_scheduled_job(job["id"])

    stored = await scheduled_job_repository.get_job(job["id"])
    assert stored is not None
    assert stored["last_outcome"] == "approval_required"
    assert stored["last_approval_id"] == "approval-123"


@pytest.mark.asyncio
async def test_execute_scheduled_job_uses_persisted_delivery_outcome(async_db):
    job = await scheduled_job_repository.create_job(
        name="Morning check",
        cron="0 9 * * *",
        timezone_name="UTC",
        target_type="message",
        content="Stand up and review priorities.",
        intervention_type="advisory",
        urgency=3,
        workflow_name="",
        workflow_args_json="",
        session_id="s1",
        created_by_session_id="s1",
    )

    async def _fake_deliver(message, **kwargs):
        message.intervention_id = "intervention-1"
        return InterventionDecision(
            action=InterventionAction.act,
            reason="scheduled",
            delivery_decision=DeliveryDecision.deliver,
            should_cost_budget=False,
        )

    with (
        patch("src.scheduler.scheduled_jobs.deliver_or_queue", AsyncMock(side_effect=_fake_deliver)),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get",
            AsyncMock(return_value=type("Intervention", (), {"latest_outcome": "failed"})()),
        ),
    ):
        await execute_scheduled_job(job["id"])

    stored = await scheduled_job_repository.get_job(job["id"])
    assert stored is not None
    assert stored["last_outcome"] == "failed"


@pytest.mark.asyncio
async def test_execute_scheduled_job_redacts_runtime_error_details(async_db):
    workflow = type("Workflow", (), {"name": "brief-sync", "tool_name": "workflow_brief_sync", "enabled": True})()
    failing_tool = MagicMock(name="workflow_brief_sync")
    failing_tool.name = "workflow_brief_sync"
    failing_tool.side_effect = RuntimeError("secret connector token leaked")

    with patch("src.scheduler.scheduled_jobs.workflow_manager.get_workflow", return_value=workflow):
        job = await scheduled_job_repository.create_job(
            name="Workflow check",
            cron="0 9 * * *",
            timezone_name="UTC",
            target_type="workflow",
            content="",
            intervention_type="advisory",
            urgency=3,
            workflow_name="brief-sync",
            workflow_args_json="{}",
            session_id="s1",
            created_by_session_id="s1",
        )
        with patch("src.agent.factory.get_tools", return_value=[failing_tool]):
            await execute_scheduled_job(job["id"])

    stored = await scheduled_job_repository.get_job(job["id"])
    assert stored is not None
    assert stored["last_outcome"] == "failed"
    assert stored["last_error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_update_scheduled_job_backfills_new_session_reference(async_db):
    job = await scheduled_job_repository.create_job(
        name="Morning check",
        cron="0 9 * * *",
        timezone_name="UTC",
        target_type="message",
        content="Stand up and review priorities.",
        intervention_type="advisory",
        urgency=3,
        workflow_name="",
        workflow_args_json="",
        session_id="s1",
        created_by_session_id="s1",
    )

    updated = await scheduled_job_repository.update_job(
        job["id"],
        session_id="s2",
    )

    assert updated is not None
    assert updated["session_id"] == "s2"

    stored = await scheduled_job_repository.get_job(job["id"])
    assert stored is not None
    assert stored["session_id"] == "s2"


@pytest.mark.asyncio
async def test_sync_scheduled_jobs_skips_invalid_rows_and_keeps_valid_jobs(async_db):
    from src.scheduler.engine import init_scheduler, shutdown_scheduler, sync_scheduled_jobs

    with patch("src.scheduler.engine.settings") as mock_settings:
        mock_settings.scheduler_enabled = True
        mock_settings.memory_consolidation_interval_min = 30
        mock_settings.goal_check_interval_hours = 4
        mock_settings.calendar_scan_interval_min = 15
        mock_settings.strategist_interval_min = 15
        mock_settings.morning_briefing_hour = 8
        mock_settings.evening_review_hour = 21
        mock_settings.activity_digest_hour = 20
        mock_settings.weekly_review_hour = 18
        mock_settings.user_timezone = "UTC"

        scheduler = init_scheduler()
        assert scheduler is not None
        try:
            async with async_db() as db:
                db.add(Session(id="s1", title="Test session"))
                await db.flush()
                db.add(
                    ScheduledJob(
                        id="broken-job",
                        name="Broken job",
                        enabled=True,
                        trigger_type="cron",
                        trigger_spec_json='{"cron":"","timezone":"UTC"}',
                        action_type="deliver_message",
                        action_spec_json='{"content":"broken","intervention_type":"advisory","urgency":3}',
                        session_id="s1",
                        created_by_session_id="s1",
                    )
                )
            job = await scheduled_job_repository.create_job(
                name="Morning check",
                cron="0 9 * * *",
                timezone_name="UTC",
                target_type="message",
                content="Stand up and review priorities.",
                intervention_type="advisory",
                urgency=3,
                workflow_name="",
                workflow_args_json="",
                session_id="s1",
                created_by_session_id="s1",
            )

            await sync_scheduled_jobs()

            dynamic_ids = {job.id for job in scheduler.get_jobs() if job.id.startswith("user_cron:")}
            assert dynamic_ids == {f"user_cron:{job['id']}"}
        finally:
            shutdown_scheduler()
