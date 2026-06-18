"""Tests for approval request APIs."""

import pytest
from unittest.mock import patch

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


@pytest.mark.asyncio
async def test_list_pending_approvals_includes_thread_labels(client):
    request = await approval_repository.get_or_create_pending(
        session_id="thread-1",
        tool_name="shell_execute",
        risk_level="high",
        summary="Calling tool: shell_execute({\"code\": \"[redacted]\"})",
        fingerprint="threaded",
        details={"resume_message": "Continue with this shell command"},
    )

    with patch(
        "src.api.approvals.session_manager.list_sessions",
        return_value=[{"id": "thread-1", "title": "Release repair"}],
    ):
        resp = await client.get("/api/approvals/pending")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload[0]["id"] == request.id
    assert payload[0]["thread_id"] == "thread-1"
    assert payload[0]["thread_label"] == "Release repair"
    assert payload[0]["resume_message"] == "Continue with this shell command"


@pytest.mark.asyncio
async def test_list_pending_approvals_includes_extension_lifecycle_context(client):
    request = await approval_repository.get_or_create_pending(
        session_id=None,
        tool_name="extension_install",
        risk_level="high",
        summary="Install extension 'Test Installable' with access to workspace_write",
        fingerprint="extension-install",
        details={
            "extension_id": "seraph.test-installable",
            "extension_display_name": "Test Installable",
            "action": "install",
            "package_path": "/tmp/extensions/test-installable",
            "permissions": {"tool_names": ["write_file"]},
            "approval_profile": {
                "requires_lifecycle_approval": True,
                "lifecycle_boundaries": ["workspace_write"],
            },
            "approval_scope": {
                "action": "install",
                "target": {
                    "type": "extension_package",
                    "name": "Test Installable",
                    "reference": "manifest.yaml",
                },
                "config_scope": {
                    "config_types": ["node_adapters"],
                    "changed_target_count": 1,
                },
            },
            "approval_context": {
                "risk_level": "high",
                "execution_boundaries": ["workspace_write"],
            },
        },
    )

    resp = await client.get("/api/approvals/pending")

    assert resp.status_code == 200
    payload = resp.json()
    approval = next(item for item in payload if item["id"] == request.id)
    assert approval["extension_id"] == "seraph.test-installable"
    assert approval["extension_display_name"] == "Test Installable"
    assert approval["extension_action"] == "install"
    assert approval["package_path"] == "/tmp/extensions/test-installable"
    assert approval["lifecycle_boundaries"] == ["workspace_write"]
    assert approval["requires_lifecycle_approval"] is True
    assert approval["permissions"] == {"tool_names": ["write_file"]}
    assert approval["approval_scope"]["target"]["reference"] == "manifest.yaml"
    assert approval["approval_scope"]["config_scope"]["config_types"] == ["node_adapters"]
    assert approval["approval_context"]["execution_boundaries"] == ["workspace_write"]
