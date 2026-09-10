import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.audit.repository import audit_repository
from src.audit.runtime import (
    log_background_task_event_sync,
    log_integration_event,
    log_integration_event_sync,
)


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


@pytest.mark.asyncio
async def test_log_integration_event_merges_runtime_and_proposal_lineage():
    proposal_lineage = {
        "proposal_id": "proposal-candidate",
        "source_content_digest": "a" * 64,
        "candidate_artifact_digest": "b" * 64,
        "candidate_handle": "prompts/candidate.md",
    }
    with patch.object(audit_repository, "log_event", new_callable=AsyncMock) as log_event:
        assert await log_integration_event(
            integration_type="self_evolution",
            name="prompt_pack",
            outcome="succeeded",
            session_id="session-candidate",
            actor="operator-candidate",
            principal_id="principal-candidate",
            details={"lineage": proposal_lineage},
        ) is True

    details = log_event.call_args.kwargs["details"]
    assert details["lineage"]["proposal_id"] == "proposal-candidate"
    assert details["lineage"]["source_content_digest"] == "a" * 64
    assert details["lineage"]["candidate_artifact_digest"] == "b" * 64
    assert details["lineage"]["candidate_handle"] == "prompts/candidate.md"
    assert details["lineage"]["principal_id"] == "principal-candidate"
    assert details["lineage"]["session_id"] == "session-candidate"
