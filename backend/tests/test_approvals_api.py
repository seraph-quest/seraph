"""Tests for approval request APIs."""

import pytest

from src.approval.repository import approval_repository


@pytest.mark.asyncio
async def test_list_pending_approvals_empty(client):
    resp = await client.get("/api/approvals/pending")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_approve_pending_request(client):
    request = await approval_repository.get_or_create_pending(
        session_id="s1",
        tool_name="shell_execute",
        risk_level="high",
        summary="Calling tool: shell_execute({\"code\": \"[redacted]\"})",
        fingerprint="abc",
        details={"arguments": {"code": "[redacted]"}, "resume_message": "run this snippet"},
    )
    resp = await client.post(f"/api/approvals/{request.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["session_id"] == "s1"
    assert resp.json()["resume_message"] == "run this snippet"


@pytest.mark.asyncio
async def test_deny_pending_request(client):
    request = await approval_repository.get_or_create_pending(
        session_id="s1",
        tool_name="get_secret",
        risk_level="high",
        summary="Calling tool: get_secret({\"key\": \"[redacted]\"})",
        fingerprint="xyz",
        details={"arguments": {"key": "[redacted]"}},
    )
    resp = await client.post(f"/api/approvals/{request.id}/deny")
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"
