"""Tests for the audit events API."""

import pytest

from src.audit.repository import audit_repository


@pytest.mark.asyncio
async def test_list_audit_events_empty(client):
    resp = await client.get("/api/audit/events")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_audit_events_returns_recent_first(client):
    await audit_repository.log_event(
        event_type="tool_call",
        tool_name="read_file",
        policy_mode="balanced",
        risk_level="low",
        summary="Calling tool: read_file({\"path\": \"README.md\"})",
        details={"arguments": {"path": "README.md"}},
    )
    await audit_repository.log_event(
        event_type="tool_result",
        tool_name="read_file",
        policy_mode="balanced",
        risk_level="low",
        summary="Read 42 lines from README.md",
    )

    resp = await client.get("/api/audit/events")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["event_type"] == "tool_result"
    assert data[1]["event_type"] == "tool_call"


@pytest.mark.asyncio
async def test_list_audit_events_filters_by_session(client):
    await audit_repository.log_event(
        event_type="tool_call",
        session_id="s1",
        tool_name="web_search",
        policy_mode="full",
        risk_level="low",
        summary="Calling tool: web_search({\"query\": \"test\"})",
    )
    await audit_repository.log_event(
        event_type="tool_call",
        session_id="s2",
        tool_name="read_file",
        policy_mode="full",
        risk_level="low",
        summary="Calling tool: read_file({\"path\": \"x\"})",
    )

    resp = await client.get("/api/audit/events", params={"session_id": "s1"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["session_id"] == "s1"
