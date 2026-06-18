import asyncio
from unittest.mock import patch

import pytest

from src.agent.session import session_manager
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.tools.audit import wrap_tools_for_audit
from src.tools.scheduled_job_tools import get_scheduled_jobs, manage_scheduled_job


@pytest.mark.asyncio
async def test_manage_scheduled_job_creates_and_lists_message_jobs(async_db):
    await session_manager.get_or_create("s1")
    tokens = set_runtime_context("s1", "off")
    try:
        created = manage_scheduled_job(
            action="create",
            name="Morning check",
            cron="0 9 * * *",
            timezone="UTC",
            target_type="message",
            content="Stand up and review priorities.",
        )
        listed = get_scheduled_jobs()
    finally:
        reset_runtime_context(tokens)

    assert "created" in created
    assert "Morning check" in listed
    assert "target=deliver_message" in listed


@pytest.mark.asyncio
async def test_manage_scheduled_job_redacts_sensitive_call_arguments(async_db):
    await session_manager.get_or_create("s1")
    audited = wrap_tools_for_audit([manage_scheduled_job])[0]
    tokens = set_runtime_context("s1", "off")
    try:
        await asyncio.to_thread(
            audited,
            action="create",
            name="Morning check",
            cron="0 9 * * *",
            timezone="UTC",
            target_type="message",
            content="top-secret scheduled payload",
        )
    finally:
        reset_runtime_context(tokens)

    events = await audit_repository.list_events(limit=10)
    manage_events = [
        event
        for event in events
        if event["tool_name"] == "manage_scheduled_job"
        and event["event_type"] in {"tool_call", "tool_result"}
    ]
    assert len(manage_events) == 2
    for event in manage_events:
        assert "top-secret scheduled payload" not in event["summary"]
        assert "top-secret scheduled payload" not in json_dump(event["details"])
        assert event["details"]["arguments"]["content_redacted"] is True
        assert event["details"]["arguments"]["target_type"] == "message"


def json_dump(value) -> str:
    import json

    return json.dumps(value, sort_keys=True)


@pytest.mark.asyncio
async def test_manage_scheduled_job_validates_workflow_targets(async_db):
    await session_manager.get_or_create("s1")
    tokens = set_runtime_context("s1", "off")
    try:
        with patch("src.scheduler.scheduled_jobs.workflow_manager.get_workflow", return_value=None):
            result = manage_scheduled_job(
                action="create",
                name="Workflow check",
                cron="0 9 * * *",
                timezone="UTC",
                target_type="workflow",
                workflow_name="missing-flow",
            )
    finally:
        reset_runtime_context(tokens)

    assert "Workflow 'missing-flow' is not available." in result


@pytest.mark.asyncio
async def test_scheduled_job_tools_are_scoped_to_the_current_session(async_db):
    await session_manager.get_or_create("s1")
    await session_manager.get_or_create("s2")

    tokens = set_runtime_context("s1", "off")
    try:
        created = manage_scheduled_job(
            action="create",
            name="Morning check",
            cron="0 9 * * *",
            timezone="UTC",
            target_type="message",
            content="Stand up and review priorities.",
        )
    finally:
        reset_runtime_context(tokens)

    job_id = created.split("job=")[1].split(",")[0]

    tokens = set_runtime_context("s2", "off")
    try:
        listed = get_scheduled_jobs()
        deleted = manage_scheduled_job(action="delete", job_id=job_id)
    finally:
        reset_runtime_context(tokens)

    assert "Morning check" not in listed
    assert "No scheduled jobs configured." in listed
    assert f"Error: Scheduled job '{job_id}' was not found." == deleted


@pytest.mark.asyncio
async def test_manage_scheduled_job_rejects_cross_session_targeting(async_db):
    await session_manager.get_or_create("s1")
    await session_manager.get_or_create("s2")

    tokens = set_runtime_context("s1", "off")
    try:
        result = manage_scheduled_job(
            action="create",
            name="Morning check",
            cron="0 9 * * *",
            timezone="UTC",
            target_type="message",
            content="Stand up and review priorities.",
            session_id="s2",
        )
    finally:
        reset_runtime_context(tokens)

    assert result == "Error: scheduled jobs cannot target a different session."


def test_manage_scheduled_job_requires_runtime_session_even_with_explicit_session_id():
    result = manage_scheduled_job(
        action="create",
        name="Morning check",
        cron="0 9 * * *",
        timezone="UTC",
        target_type="message",
        content="Stand up and review priorities.",
        session_id="s1",
    )

    assert result == "Error: scheduled jobs require an active session."


@pytest.mark.asyncio
async def test_manage_scheduled_job_update_keeps_existing_session_binding(async_db):
    await session_manager.get_or_create("s1")

    tokens = set_runtime_context("s1", "off")
    try:
        created = manage_scheduled_job(
            action="create",
            name="Morning check",
            cron="0 9 * * *",
            timezone="UTC",
            target_type="message",
            content="Stand up and review priorities.",
        )
        job_id = created.split("job=")[1].split(",")[0]
        updated = manage_scheduled_job(
            action="update",
            job_id=job_id,
            cron="30 9 * * *",
        )
        listed = get_scheduled_jobs()
    finally:
        reset_runtime_context(tokens)

    assert "updated" in updated
    assert "session=s1" in listed


@pytest.mark.asyncio
async def test_manage_scheduled_job_reports_sync_degradation_without_raising(async_db):
    await session_manager.get_or_create("s1")

    tokens = set_runtime_context("s1", "off")
    try:
        with patch(
            "src.tools.scheduled_job_tools.sync_scheduled_jobs_blocking",
            side_effect=RuntimeError("scheduler unhealthy"),
        ):
            result = manage_scheduled_job(
                action="create",
                name="Morning check",
                cron="0 9 * * *",
                timezone="UTC",
                target_type="message",
                content="Stand up and review priorities.",
            )
    finally:
        reset_runtime_context(tokens)

    assert "created" in result
    assert "Scheduler sync is degraded" in result


@pytest.mark.asyncio
async def test_manage_scheduled_job_rejects_non_integer_urgency(async_db):
    await session_manager.get_or_create("s1")

    tokens = set_runtime_context("s1", "off")
    try:
        result = manage_scheduled_job(
            action="create",
            name="Morning check",
            cron="0 9 * * *",
            timezone="UTC",
            target_type="message",
            content="Stand up and review priorities.",
            urgency="high",
        )
    finally:
        reset_runtime_context(tokens)

    assert result == "Error: urgency must be an integer."
