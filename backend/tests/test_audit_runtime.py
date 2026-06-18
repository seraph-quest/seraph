import asyncio

from src.audit.repository import audit_repository
from src.audit.runtime import log_background_task_event_sync, log_integration_event_sync


def test_log_integration_event_sync_persists_without_running_loop(async_db):
    log_integration_event_sync(
        integration_type="browser",
        name="playwright",
        outcome="succeeded",
        details={"hostname": "example.com", "action": "extract"},
    )

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [event for event in events if event["event_type"] == "integration_succeeded"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "browser:playwright"
    assert events[0]["details"]["hostname"] == "example.com"


def test_log_background_task_event_sync_persists_without_running_loop(async_db):
    log_background_task_event_sync(
        task_name="nightly-refresh",
        outcome="succeeded",
        session_id="session-1",
        details={"source": "test"},
    )

    async def _fetch():
        events = await audit_repository.list_events(limit=5, session_id="session-1")
        return [event for event in events if event["event_type"] == "background_task_succeeded"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "nightly-refresh"
    assert events[0]["details"]["source"] == "test"
