"""Tests for approval request APIs."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from unittest.mock import AsyncMock, patch

from src.approval.repository import approval_repository
from src.auth.service import test_bypass_operator as _test_bypass_operator
from src.db import engine as db_engine
from src.db.models import ApprovalRequest


def _approval_request(operator) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/approvals/test/approve",
            "headers": [],
            "query_string": b"",
            "state": {"operator": operator},
        }
    )


@pytest.mark.asyncio
async def test_list_pending_approvals_empty(client):
    resp = await client.get("/api/approvals/pending")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_pending_approvals_rejects_anonymous_call(client):
    with patch(
        "src.api.approvals._require_approval_operator",
        side_effect=HTTPException(status_code=401, detail={"code": "authentication_required"}),
    ):
        resp = await client.get("/api/approvals/pending")

    assert resp.status_code == 401
    assert resp.json()["detail"] == {"code": "authentication_required"}


@pytest.mark.asyncio
async def test_approval_deadline_defaults_and_explicit_deadline_is_preserved(async_db):
    now = datetime.now(timezone.utc)
    default_request = await approval_repository.get_or_create_pending(
        session_id="deadline-default",
        tool_name="shell_execute",
        risk_level="high",
        summary="Bounded approval",
        fingerprint="deadline-default",
    )
    assert default_request.expires_at is not None
    default_expiry = default_request.expires_at
    if default_expiry.tzinfo is None:
        default_expiry = default_expiry.replace(tzinfo=timezone.utc)
    assert now < default_expiry <= now + timedelta(minutes=5, seconds=2)

    explicit_expiry = now + timedelta(hours=2)
    explicit_request = await approval_repository.get_or_create_pending(
        session_id="deadline-explicit",
        tool_name="shell_execute",
        risk_level="high",
        summary="Explicit deadline",
        fingerprint="deadline-explicit",
        expires_at=explicit_expiry,
    )
    assert explicit_request.expires_at is not None
    stored_expiry = explicit_request.expires_at
    if stored_expiry.tzinfo is None:
        stored_expiry = stored_expiry.replace(tzinfo=timezone.utc)
    assert stored_expiry == explicit_expiry.replace(microsecond=explicit_expiry.microsecond)


@pytest.mark.asyncio
async def test_missing_or_expired_deadline_cannot_resolve_or_consume(async_db):
    now = datetime.now(timezone.utc)
    expired = await approval_repository.get_or_create_pending(
        session_id="deadline-expired",
        tool_name="shell_execute",
        risk_level="high",
        summary="Expired approval",
        fingerprint="deadline-expired",
        expires_at=now - timedelta(seconds=1),
    )
    expired_resolution = await approval_repository.resolve_with_metadata(
        expired.id,
        "approved",
        now=now,
    )
    assert expired_resolution.transitioned is False
    assert expired_resolution.reason == "expired"
    assert expired_resolution.request is not None
    assert expired_resolution.request.status == "pending"

    missing = await approval_repository.get_or_create_pending(
        session_id="deadline-missing",
        tool_name="shell_execute",
        risk_level="high",
        summary="Legacy approval",
        fingerprint="deadline-missing",
    )
    async with db_engine.get_session() as db:
        stored = await db.get(ApprovalRequest, missing.id)
        assert stored is not None
        stored.expires_at = None
        await db.flush()
    missing_resolution = await approval_repository.resolve_with_metadata(
        missing.id,
        "denied",
        now=now,
    )
    assert missing_resolution.transitioned is False
    assert missing_resolution.reason == "expiry_missing"
    assert missing_resolution.request is not None
    assert missing_resolution.request.status == "pending"

    async with db_engine.get_session() as db:
        stored = await db.get(ApprovalRequest, missing.id)
        assert stored is not None
        stored.status = "approved"
        stored.expires_at = now - timedelta(seconds=1)
        await db.flush()
    assert await approval_repository.consume_approved(
        session_id="deadline-missing",
        tool_name="shell_execute",
        fingerprint="deadline-missing",
    ) is False
    async with db_engine.get_session() as db:
        stored = await db.get(ApprovalRequest, missing.id)
        assert stored is not None
        assert stored.status == "approved"


@pytest.mark.asyncio
async def test_terminal_approval_is_a_conflict_without_duplicate_audit(client):
    request = await approval_repository.get_or_create_pending(
        session_id="test-auth-bypass",
        tool_name="shell_execute",
        risk_level="high",
        summary="Resolve once",
        fingerprint="resolve-once",
    )
    with patch("src.api.approvals.audit_repository.log_event", new_callable=AsyncMock) as audit:
        first = await client.post(f"/api/approvals/{request.id}/approve")
        second = await client.post(f"/api/approvals/{request.id}/approve")

    assert first.status_code == 200
    assert first.json()["transitioned"] is True
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "approval_already_resolved"
    assert second.json()["detail"]["transitioned"] is False
    assert audit.await_count == 1


@pytest.mark.asyncio
async def test_expired_approval_decision_has_no_audit_or_terminal_effect(client):
    expired = await approval_repository.get_or_create_pending(
        session_id="test-auth-bypass",
        tool_name="shell_execute",
        risk_level="high",
        summary="Expired decision",
        fingerprint="expired-decision",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with patch("src.api.approvals.audit_repository.log_event", new_callable=AsyncMock) as audit:
        response = await client.post(f"/api/approvals/{expired.id}/deny")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_expired"
    assert audit.await_count == 0
    stored = await approval_repository.get(expired.id)
    assert stored is not None
    assert stored.status == "pending"


@pytest.mark.asyncio
async def test_list_pending_approvals_filters_rows_to_authenticated_owner(client):
    owner = _test_bypass_operator()
    await approval_repository.get_or_create_pending(
        session_id="owned-conversation",
        tool_name="shell_execute",
        risk_level="high",
        summary="Owned approval",
        fingerprint="owned-listing",
        details={
            "approval_owner_operator_session_id": owner.session_id,
            "approval_owner_principal_id": owner.principal.principal_id,
        },
    )
    foreign = await approval_repository.get_or_create_pending(
        session_id="foreign-conversation",
        tool_name="shell_execute",
        risk_level="high",
        summary="Foreign approval",
        fingerprint="foreign-listing",
        details={
            "approval_owner_operator_session_id": "other-auth-session",
            "approval_owner_principal_id": "operator:other",
        },
    )

    resp = await client.get("/api/approvals/pending")

    assert resp.status_code == 200
    payload = resp.json()
    assert any(item["summary"] == "Owned approval" for item in payload)
    assert all(item["id"] != foreign.id for item in payload)


@pytest.mark.asyncio
async def test_approve_pending_request(client):
    request = await approval_repository.get_or_create_pending(
        session_id="test-auth-bypass",
        tool_name="shell_execute",
        risk_level="high",
        summary="Calling tool: shell_execute({\"code\": \"[redacted]\"})",
        fingerprint="abc",
        details={"arguments": {"code": "[redacted]"}, "resume_message": "run this snippet"},
    )
    resp = await client.post(f"/api/approvals/{request.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["session_id"] == "test-auth-bypass"
    assert resp.json()["resume_message"] == "run this snippet"


@pytest.mark.asyncio
async def test_deny_pending_request(client):
    request = await approval_repository.get_or_create_pending(
        session_id="test-auth-bypass",
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
async def test_approval_decision_rejects_cross_session_operator(async_db):
    from src.api.approvals import approve_request

    owner = _test_bypass_operator()
    request = await approval_repository.get_or_create_pending(
        session_id=owner.session_id,
        tool_name="extension_install",
        risk_level="high",
        summary="Install extension",
        fingerprint="cross-session",
        details={"approval_owner_session_id": owner.session_id},
    )
    other = replace(
        owner,
        session_id="other-session",
        principal=replace(owner.principal, session_id="other-session"),
    )

    with pytest.raises(HTTPException) as error:
        await approve_request(request.id, _approval_request(other))

    assert error.value.status_code == 403
    assert error.value.detail == {"code": "approval_owner_mismatch"}
    pending = await approval_repository.get(request.id)
    assert pending is not None
    assert pending.status == "pending"


@pytest.mark.asyncio
async def test_approval_owner_uses_auth_session_separate_from_conversation(async_db):
    from src.api.approvals import approve_request, deny_request

    owner = _test_bypass_operator()
    request = await approval_repository.get_or_create_pending(
        session_id="conversation-owner-1",
        tool_name="extension_install",
        risk_level="high",
        summary="Install extension",
        fingerprint="auth-owner-1",
        details={
            "approval_conversation_id": "conversation-owner-1",
            "approval_owner_operator_session_id": owner.session_id,
            "approval_owner_principal_id": owner.principal.principal_id,
        },
    )

    approved = await approve_request(request.id, _approval_request(owner))
    assert approved["status"] == "approved"
    resolved = await approval_repository.get(request.id)
    assert resolved is not None
    assert resolved.session_id == "conversation-owner-1"

    cross_owner = replace(
        owner,
        session_id="other-auth-session",
        principal=replace(
            owner.principal,
            principal_id="operator:other",
            session_id="other-auth-session",
            operator_session_id="other-auth-session",
        ),
    )
    cross_request = await approval_repository.get_or_create_pending(
        session_id="conversation-owner-2",
        tool_name="extension_install",
        risk_level="high",
        summary="Install extension",
        fingerprint="auth-owner-2",
        details={
            "approval_conversation_id": "conversation-owner-2",
            "approval_owner_operator_session_id": owner.session_id,
            "approval_owner_principal_id": owner.principal.principal_id,
        },
    )

    with pytest.raises(HTTPException) as cross_error:
        await deny_request(cross_request.id, _approval_request(cross_owner))

    assert cross_error.value.status_code == 403
    assert cross_error.value.detail == {"code": "approval_owner_mismatch"}
    pending = await approval_repository.get(cross_request.id)
    assert pending is not None
    assert pending.status == "pending"

    revoked_owner = replace(
        owner,
        principal=replace(owner.principal, revoked=True),
    )
    revoked_request = await approval_repository.get_or_create_pending(
        session_id="conversation-owner-3",
        tool_name="extension_install",
        risk_level="high",
        summary="Install extension",
        fingerprint="auth-owner-3",
        details={
            "approval_conversation_id": "conversation-owner-3",
            "approval_owner_operator_session_id": owner.session_id,
            "approval_owner_principal_id": owner.principal.principal_id,
        },
    )

    with pytest.raises(HTTPException) as revoked_error:
        await approve_request(revoked_request.id, _approval_request(revoked_owner))

    assert revoked_error.value.status_code == 401
    assert revoked_error.value.detail == {"code": "authentication_required"}
    pending = await approval_repository.get(revoked_request.id)
    assert pending is not None
    assert pending.status == "pending"


@pytest.mark.asyncio
async def test_list_pending_approvals_includes_thread_labels(client):
    owner = _test_bypass_operator()
    request = await approval_repository.get_or_create_pending(
        session_id="thread-1",
        tool_name="shell_execute",
        risk_level="high",
        summary="Calling tool: shell_execute({\"code\": \"[redacted]\"})",
        fingerprint="threaded",
        details={
            "resume_message": "Continue with this shell command",
            "approval_owner_operator_session_id": owner.session_id,
            "approval_owner_principal_id": owner.principal.principal_id,
        },
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
    owner = _test_bypass_operator()
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
            "approval_owner_operator_session_id": owner.session_id,
            "approval_owner_principal_id": owner.principal.principal_id,
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
