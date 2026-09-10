"""Focused proof for the canonical conversation and durable native outbox slice."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import json
import os
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from config.settings import settings
from src.conversation.identity import (
    ConversationIdentityError,
    build_conversation_identity,
    build_lineage,
    issue_attachment_quarantine_receipt,
    redact_attachment_refs,
    validate_attachment_refs,
)
from src.db import engine as db_engine
from src.db.models import (
    ApprovalRequest,
    NativeNotificationOutbox,
    OperatorSession,
    Session,
)
from src.approval.repository import approval_repository
from src.models.schemas import WSResponse
from src.observer.delivery import _canonical_delivery_message, _resolve_delivery_identity
from src.observer.native_notification_queue import NativeNotificationQueue
from src.scheduler.connection_manager import ConnectionManager
from src.security.trust_contract import TrustPrincipal


@pytest.fixture(autouse=True)
def _attachment_receipt_secret(monkeypatch):
    """Give this isolated proof suite a deterministic server-owned receipt key."""

    monkeypatch.setattr(settings, "operator_auth_secret", "conversation-750-test-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", True)


def _attachment_ref(
    *,
    attachment_id: str,
    owner_principal_id: str,
    content_hash: str,
    media_type: str,
    **extra,
) -> dict:
    receipt = issue_attachment_quarantine_receipt(
        attachment_id=attachment_id,
        owner_principal_id=owner_principal_id,
        content_hash=content_hash,
        media_type=media_type,
        size_bytes=extra.get("size_bytes"),
        duration_seconds=extra.get("duration_seconds"),
        voice_note=extra.get("voice_note"),
    )
    return {
        "attachment_id": attachment_id,
        "media_type": media_type,
        "content_hash": content_hash,
        "quarantine_status": "quarantined",
        "quarantine_receipt": receipt,
        **extra,
    }


def test_conversation_identity_is_single_session_and_attachment_refs_are_public_metadata():
    identity = build_conversation_identity(
        conversation_id="conversation-750",
        thread_id="conversation-750",
        owner_principal_id="operator:750",
        operator_session_id="operator-session-750",
        channel="web",
        transport="websocket",
        correlation_id="corr-750",
        causation_id="message-750",
    )
    lineage = build_lineage(
        identity,
        message_id="assistant-750",
        attachment_refs=[
            _attachment_ref(
                attachment_id="attachment-1",
                owner_principal_id="operator:750",
                media_type="image/png",
                content_hash="sha256:abc",
                size_bytes=42,
                file_path="/private/should-not-persist.png",
                token="provider-secret",
            )
        ],
    )

    assert identity.conversation_id == identity.thread_id == "conversation-750"
    assert lineage["conversation_id"] == lineage["thread_id"] == "conversation-750"
    attachment = lineage["attachment_refs"][0]
    assert attachment["attachment_id"] == "attachment-1"
    assert attachment["owner_principal_id"] == "operator:750"
    assert attachment["content_hash"] == "sha256:abc"
    assert attachment["quarantine_status"] == "quarantined"
    assert attachment["quarantine_receipt_digest"]
    assert "file_path" not in json.dumps(lineage)
    assert "provider-secret" not in json.dumps(lineage)

    with pytest.raises(ConversationIdentityError, match="Thread identity"):
        build_conversation_identity(
            conversation_id="conversation-750",
            thread_id="forged-thread",
            owner_principal_id="operator:750",
        )
    with pytest.raises(ConversationIdentityError, match="channel"):
        build_conversation_identity(
            conversation_id="conversation-750",
            owner_principal_id="operator:750",
            channel="forged-channel",
        )
    assert redact_attachment_refs([{"file_path": "/private/only"}]) == [{}]
    frame = WSResponse(
        type="final",
        session_id=identity.conversation_id,
        conversation_id=identity.conversation_id,
        thread_id=identity.thread_id,
        owner_principal_id=identity.owner_principal_id,
        operator_session_id=identity.operator_session_id,
    )
    assert frame.conversation_id == frame.thread_id == frame.session_id
    assert frame.owner_principal_id == "operator:750"


def test_attachment_owner_conflict_and_forged_delivery_identity_are_rejected():
    with pytest.raises(ConversationIdentityError, match="ownership"):
        redact_attachment_refs(
            [{"attachment_id": "attachment-750", "owner_principal_id": "operator:other"}],
            owner_principal_id="operator:750",
        )

    trusted = TrustPrincipal(
        principal_id="operator:750",
        principal_type="operator",
        session_id="conversation-750",
        operator_session_id="operator-session-750",
    )
    forged = WSResponse(
        type="proactive",
        session_id="conversation-750",
        owner_principal_id="operator:other",
        operator_session_id="operator-session-750",
    )
    with pytest.raises(ConversationIdentityError, match="owner"):
        _resolve_delivery_identity(
            forged,
            session_id="conversation-750",
            trusted_principal=trusted,
        )
    with pytest.raises(ConversationIdentityError, match="authenticated runtime principal"):
        _resolve_delivery_identity(
            WSResponse(
                type="proactive",
                session_id="conversation-750",
                owner_principal_id="operator:750",
            ),
            session_id="conversation-750",
            trusted_principal=None,
        )
    with pytest.raises(ConversationIdentityError, match="quarantine"):
        validate_attachment_refs(
            [
                {
                    "attachment_id": "unknown-attachment",
                    "content_hash": "sha256:forged",
                    "quarantine_status": "quarantined",
                    "media_type": "text/plain",
                }
            ],
            owner_principal_id="operator:750",
        )
    with pytest.raises(ConversationIdentityError, match="quarantine"):
        _resolve_delivery_identity(
            WSResponse(
                type="proactive",
                session_id="conversation-750",
                owner_principal_id="operator:750",
                attachment_refs=[{"attachment_id": "unknown-attachment"}],
            ),
            session_id="conversation-750",
            trusted_principal=trusted,
        )

    valid = _attachment_ref(
        attachment_id="attachment-750",
        owner_principal_id="operator:750",
        content_hash="sha256:valid",
        media_type="text/plain",
    )
    tampered = {**valid, "content_hash": "sha256:forged"}
    with pytest.raises(ConversationIdentityError, match="receipt"):
        validate_attachment_refs([tampered], owner_principal_id="operator:750")
    wrong_key = valid["quarantine_receipt"][:-1] + ("0" if valid["quarantine_receipt"][-1] != "0" else "1")
    with pytest.raises(ConversationIdentityError, match="receipt"):
        validate_attachment_refs(
            [{**valid, "quarantine_receipt": wrong_key}],
            owner_principal_id="operator:750",
        )


def test_delivery_preserves_source_adapter_channel_and_transport_lineage():
    trusted = TrustPrincipal(
        principal_id="operator:telegram",
        principal_type="operator",
        session_id="conversation-telegram",
        operator_session_id="operator-session-telegram",
    )
    message = WSResponse(
        type="proactive",
        session_id="conversation-telegram",
        conversation_id="conversation-telegram",
        thread_id="conversation-telegram",
        owner_principal_id="operator:telegram",
        operator_session_id="operator-session-telegram",
        channel="telegram",
        transport="telegram",
        correlation_id="telegram:corr-1",
    )
    identity, conversation_id, owner, operator_session_id = _resolve_delivery_identity(
        message,
        session_id="conversation-telegram",
        trusted_principal=trusted,
    )
    assert identity is not None
    assert identity.channel == "telegram"
    assert identity.transport == "telegram"
    assert conversation_id == identity.conversation_id == "conversation-telegram"
    assert owner == "operator:telegram"
    assert operator_session_id == "operator-session-telegram"


def test_scheduler_service_envelope_can_bind_an_explicit_session():
    service = TrustPrincipal(
        principal_id="service:scheduler:test",
        principal_type="service",
        job_id="scheduler:test:run",
    )
    message = WSResponse(type="proactive", session_id="conversation-scheduled")
    identity, conversation_id, owner, _ = _resolve_delivery_identity(
        message,
        session_id="conversation-scheduled",
        trusted_principal=service,
    )
    assert identity is not None
    assert conversation_id == "conversation-scheduled"
    assert owner == "service:scheduler:test"


def test_transport_frame_uses_canonical_lineage_and_never_forwards_receipt_token():
    trusted = TrustPrincipal(
        principal_id="operator:750",
        principal_type="operator",
        session_id="conversation-750",
        operator_session_id="operator-session-750",
    )
    raw_ref = _attachment_ref(
        attachment_id="attachment-frame-750",
        owner_principal_id="operator:750",
        content_hash="sha256:frame",
        media_type="text/plain",
    )
    message = WSResponse(
        type="proactive",
        content="safe frame",
        session_id="conversation-750",
        owner_principal_id="operator:750",
        attachment_refs=[raw_ref],
    )
    identity, conversation_id, owner, _ = _resolve_delivery_identity(
        message,
        session_id="conversation-750",
        trusted_principal=trusted,
    )
    frame = _canonical_delivery_message(
        message,
        identity=identity,
        requested_conversation_id=conversation_id,
        owner_principal_id=owner,
    )
    assert frame.session_id == frame.conversation_id == frame.thread_id == "conversation-750"
    assert frame.attachment_refs[0]["attachment_id"] == "attachment-frame-750"
    assert '"quarantine_receipt":' not in json.dumps(frame.model_dump())


def test_attachment_receipt_rejects_a_server_key_change(monkeypatch):
    valid = _attachment_ref(
        attachment_id="attachment-key-750",
        owner_principal_id="operator:750",
        content_hash="sha256:key-check",
        media_type="text/plain",
    )
    monkeypatch.setattr(settings, "operator_auth_secret", "different-server-secret")
    with pytest.raises(ConversationIdentityError, match="signature"):
        validate_attachment_refs([valid], owner_principal_id="operator:750")


def test_expired_attachment_receipt_is_rejected_on_revalidation():
    now = datetime.now(timezone.utc)
    receipt = issue_attachment_quarantine_receipt(
        attachment_id="expired-attachment",
        owner_principal_id="operator:750",
        content_hash="sha256:expired",
        media_type="text/plain",
        issued_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=1),
    )
    with pytest.raises(ConversationIdentityError, match="expired"):
        validate_attachment_refs(
            [{
                "attachment_id": "expired-attachment",
                "owner_principal_id": "operator:750",
                "content_hash": "sha256:expired",
                "quarantine_status": "quarantined",
                "quarantine_receipt": receipt,
            }],
            owner_principal_id="operator:750",
        )


def test_bound_websocket_broadcast_filters_owner_conversation_and_redacts_receipt():
    class FakeWebSocket:
        def __init__(self):
            self.payloads = []

        async def send_text(self, payload):
            self.payloads.append(payload)

    async def exercise():
        manager = ConnectionManager()
        owner_a = FakeWebSocket()
        owner_b = FakeWebSocket()
        manager.connect(owner_a, owner_principal_id="operator:a", operator_session_id="auth:a")
        manager.connect(owner_b, owner_principal_id="operator:b", operator_session_id="auth:b")
        manager.bind_conversation(owner_a, "conversation:a")
        manager.bind_conversation(owner_b, "conversation:b")
        raw_ref = _attachment_ref(
            attachment_id="attachment-ws-750",
            owner_principal_id="operator:a",
            content_hash="sha256:ws",
            media_type="text/plain",
        )
        result = await manager.broadcast(
            WSResponse(
                type="proactive",
                content="private",
                session_id="conversation:a",
                conversation_id="conversation:a",
                thread_id="conversation:a",
                owner_principal_id="operator:a",
                operator_session_id="auth:a",
                attachment_refs=[raw_ref],
            )
        )
        return result, owner_a, owner_b, raw_ref

    result, owner_a, owner_b, raw_ref = asyncio.run(exercise())
    assert result.attempted_connections == 1
    assert result.delivered_connections == 1
    assert len(owner_a.payloads) == 1
    assert owner_b.payloads == []
    assert raw_ref["quarantine_receipt"] not in owner_a.payloads[0]
    assert "quarantine_receipt_digest" in owner_a.payloads[0]


@pytest.mark.asyncio
async def test_approval_persistence_rejects_forged_attachment_metadata(async_db):
    common = dict(
        session_id="conversation-approval-750",
        tool_name="attachment_tool",
        risk_level="medium",
        summary="Attachment approval",
        fingerprint="approval-fingerprint-750",
        details={
            "owner_principal_id": "operator:750",
            "operator_session_id": "operator-session-750",
            "attachment_refs": [
                {
                    "attachment_id": "forged-attachment",
                    "content_hash": "sha256:forged",
                    "quarantine_status": "quarantined",
                }
            ],
        },
    )
    with pytest.raises(ConversationIdentityError, match="receipt"):
        await approval_repository.get_or_create_pending(**common)

    valid = _attachment_ref(
        attachment_id="approved-attachment",
        owner_principal_id="operator:750",
        content_hash="sha256:approved",
        media_type="text/plain",
    )
    accepted = await approval_repository.get_or_create_pending(
        **{
            **common,
            "fingerprint": "approval-fingerprint-accepted-750",
            "details": {
                **common["details"],
                "attachment_refs": [valid],
            },
        }
    )
    persisted = json.loads(accepted.attachment_refs_json)
    assert persisted[0]["attachment_id"] == "approved-attachment"
    assert persisted[0]["quarantine_receipt_digest"]
    assert '"quarantine_receipt":' not in accepted.attachment_refs_json
    assert "forged-attachment" not in accepted.attachment_refs_json


@pytest.mark.asyncio
async def test_session_bound_outbox_rejects_ownerless_intent():
    queue = NativeNotificationQueue()
    with pytest.raises(ConversationIdentityError, match="owner"):
        await queue.enqueue(
            intervention_id="ownerless-intervention",
            title="Bound notification",
            body="Must be rejected",
            intervention_type="alert",
            urgency=3,
            session_id="ownerless-conversation",
        )


@pytest.mark.asyncio
async def test_rest_payload_persists_the_same_identity_and_redacted_attachment_refs(async_db, client):
    agent = MagicMock()
    agent.run.return_value = "Identity is durable"
    with (
        patch("src.auth.middleware.auth_enabled", return_value=False),
        patch("src.api.chat.create_onboarding_agent", return_value=agent),
        patch("src.api.chat.should_use_direct_local_chat", return_value=False),
    ):
        response = await client.post(
            "/api/chat",
            json={
                "message": "Cross surface identity",
                "message_id": "cross-surface-message-750",
                "attachments": [
                        _attachment_ref(
                            attachment_id="attachment-750",
                            owner_principal_id="operator:test-bypass",
                        media_type="text/plain",
                        content_hash="sha256:attachment-750",
                        file_path="/private/file.txt",
                        token="private-token",
                    )
                ],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation_id"] == payload["thread_id"] == payload["session_id"]
    assert payload["owner_principal_id"]
    assert payload["attachment_refs"][0]["attachment_id"] == "attachment-750"
    assert payload["attachment_refs"][0]["owner_principal_id"] == "operator:test-bypass"
    assert payload["attachment_refs"][0]["quarantine_receipt_digest"]
    with patch("src.auth.middleware.auth_enabled", return_value=False):
        history = await client.get(f"/api/sessions/{payload['session_id']}/messages")
    assert history.status_code == 200
    history_message = history.json()[-1]
    assert history_message["conversation_id"] == payload["conversation_id"]
    assert history_message["thread_id"] == payload["thread_id"]
    assert history_message["owner_principal_id"] == payload["owner_principal_id"]
    assert history_message["attachment_refs"] == payload["attachment_refs"]
    assert "private-token" not in json.dumps(history.json())


@pytest_asyncio.fixture
async def file_db(tmp_path, monkeypatch):
    """Use a real temporary SQLite file through the production queue seam."""
    database_path = tmp_path / "seraph.db"
    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with test_engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def get_file_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(db_engine, "get_session", get_file_session)
    yield get_file_session, database_path
    await test_engine.dispose()


async def _add_owner(get_session, *, session_id: str, owner_id: str, operator_session_id: str, revoked_at=None):
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        db.add(Session(id=session_id, owner_principal_id=owner_id))
        db.add(
            OperatorSession(
                id=operator_session_id,
                token_hash=f"hash-{operator_session_id}",
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=1),
                revoked_at=revoked_at,
            )
        )


@pytest.mark.asyncio
async def test_approval_expiry_and_atomic_consume_replay(file_db, monkeypatch):
    get_session, _ = file_db
    monkeypatch.setattr("src.approval.repository.get_session", get_session)
    await _add_owner(
        get_session,
        session_id="conversation-approval-expiry",
        owner_id="operator:approval",
        operator_session_id="operator-session-approval",
    )
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    request = await approval_repository.get_or_create_pending(
        session_id="conversation-approval-expiry",
        tool_name="approval-tool",
        risk_level="high",
        summary="One-use approval",
        fingerprint="approval-expiry-fingerprint",
        details={
            "owner_principal_id": "operator:approval",
            "approval_owner_operator_session_id": "operator-session-approval",
            "expires_at": expires_at,
        },
    )
    approved = await approval_repository.resolve(request.id, "approved")
    assert approved is not None and approved.status == "approved"
    assert await approval_repository.has_approved(
        session_id=request.session_id,
        tool_name=request.tool_name,
        fingerprint=request.fingerprint,
        owner_operator_session_id="operator-session-approval",
    )
    results = await asyncio.gather(
        approval_repository.consume_approved(
            session_id=request.session_id,
            tool_name=request.tool_name,
            fingerprint=request.fingerprint,
            owner_operator_session_id="operator-session-approval",
        ),
        approval_repository.consume_approved(
            session_id=request.session_id,
            tool_name=request.tool_name,
            fingerprint=request.fingerprint,
            owner_operator_session_id="operator-session-approval",
        ),
    )
    assert sum(bool(result) for result in results) == 1
    assert not await approval_repository.has_approved(
        session_id=request.session_id,
        tool_name=request.tool_name,
        fingerprint=request.fingerprint,
        owner_operator_session_id="operator-session-approval",
    )

    expired_request = await approval_repository.get_or_create_pending(
        session_id="conversation-approval-expiry",
        tool_name="approval-tool",
        risk_level="high",
        summary="Expired approval",
        fingerprint="approval-expired-fingerprint",
        details={
            "owner_principal_id": "operator:approval",
            "approval_owner_operator_session_id": "operator-session-approval",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp(),
        },
    )
    await approval_repository.resolve(expired_request.id, "approved")
    async with get_session() as db:
        row = (await db.execute(
            select(ApprovalRequest).where(ApprovalRequest.id == expired_request.id)
        )).scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not await approval_repository.consume_approved(
        session_id=expired_request.session_id,
        tool_name=expired_request.tool_name,
        fingerprint=expired_request.fingerprint,
        owner_operator_session_id="operator-session-approval",
    )


@pytest.mark.asyncio
async def test_approved_consume_revalidates_attachment_receipt_at_execution(file_db, monkeypatch):
    get_session, _ = file_db
    monkeypatch.setattr("src.approval.repository.get_session", get_session)
    await _add_owner(
        get_session,
        session_id="conversation-approval-attachment-expiry",
        owner_id="operator:approval-attachment",
        operator_session_id="operator-session-approval-attachment",
    )
    now = datetime.now(timezone.utc)
    receipt = issue_attachment_quarantine_receipt(
        attachment_id="approval-expiring-attachment",
        owner_principal_id="operator:approval-attachment",
        content_hash="sha256:approval-expiring",
        media_type="text/plain",
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(milliseconds=250),
    )
    attachment_refs = validate_attachment_refs(
        [{
            "attachment_id": "approval-expiring-attachment",
            "owner_principal_id": "operator:approval-attachment",
            "content_hash": "sha256:approval-expiring",
            "media_type": "text/plain",
            "quarantine_status": "quarantined",
            "quarantine_receipt": receipt,
        }],
        owner_principal_id="operator:approval-attachment",
    )
    approval_id = "approval-attachment-expiry"
    async with get_session() as db:
        db.add(
            ApprovalRequest(
                id=approval_id,
                session_id="conversation-approval-attachment-expiry",
                owner_principal_id="operator:approval-attachment",
                operator_session_id="operator-session-approval-attachment",
                attachment_refs_json=json.dumps(attachment_refs),
                status="approved",
                tool_name="attachment-tool",
                fingerprint="approval-attachment-expiry-fingerprint",
                summary="Attachment approval",
                expires_at=now + timedelta(minutes=5),
                details_json=json.dumps({
                    "owner_principal_id": "operator:approval-attachment",
                    "approval_owner_operator_session_id": "operator-session-approval-attachment",
                    "attachment_refs": attachment_refs,
                }),
            )
        )

    await asyncio.sleep(0.4)
    assert not await approval_repository.consume_approved(
        session_id="conversation-approval-attachment-expiry",
        tool_name="attachment-tool",
        fingerprint="approval-attachment-expiry-fingerprint",
        owner_operator_session_id="operator-session-approval-attachment",
    )
    async with get_session() as db:
        row = (
            await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
        ).scalar_one()
        details = json.loads(row.details_json or "{}")
        assert row.status == "expired"
        assert row.attachment_refs_json == "[]"
        assert details["attachment_refs"] == []
        assert details["attachment_refs_status"] == "expired"


@pytest.mark.asyncio
async def test_resume_consume_revalidates_attachment_receipt_at_execution(file_db, monkeypatch):
    get_session, _ = file_db
    monkeypatch.setattr("src.approval.repository.get_session", get_session)
    await _add_owner(
        get_session,
        session_id="conversation-resume-attachment-expiry",
        owner_id="operator:resume-attachment",
        operator_session_id="operator-session-resume-attachment",
    )
    now = datetime.now(timezone.utc)
    receipt = issue_attachment_quarantine_receipt(
        attachment_id="resume-expiring-attachment",
        owner_principal_id="operator:resume-attachment",
        content_hash="sha256:resume-expiring",
        media_type="text/plain",
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(milliseconds=250),
    )
    attachment_refs = validate_attachment_refs(
        [{
            "attachment_id": "resume-expiring-attachment",
            "owner_principal_id": "operator:resume-attachment",
            "content_hash": "sha256:resume-expiring",
            "media_type": "text/plain",
            "quarantine_status": "quarantined",
            "quarantine_receipt": receipt,
        }],
        owner_principal_id="operator:resume-attachment",
    )
    approval_id = "approval-resume-attachment-expiry"
    approval_expires_at = (now + timedelta(minutes=5)).timestamp()
    details = {
        "approval_owner_operator_session_id": "operator-session-resume-attachment",
        "approval_operator_principal_id": "operator:resume-attachment",
        "durable_job_id": "job-resume-attachment",
        "durable_owner_kind": "operator",
        "durable_owner_principal_id": "operator:resume-attachment",
        "durable_service_id": "service:resume-attachment",
        "durable_approval_id": approval_id,
        "durable_authority_digest": "authority-resume-attachment",
        "durable_goal_id": "goal-resume-attachment",
        "durable_goal_revision": 1,
        "durable_plan_revision": 1,
        "durable_capability_version": "capability-resume-attachment-v1",
        "durable_budget_digest": "budget-resume-attachment",
        "approval_expires_at": approval_expires_at,
        "attachment_refs": attachment_refs,
    }
    async with get_session() as db:
        db.add(
            ApprovalRequest(
                id=approval_id,
                session_id="conversation-resume-attachment-expiry",
                owner_principal_id="operator:resume-attachment",
                operator_session_id="operator-session-resume-attachment",
                attachment_refs_json=json.dumps(attachment_refs),
                status="approved",
                tool_name="resume-tool",
                fingerprint="resume-attachment-expiry-fingerprint",
                summary="Resume attachment approval",
                expires_at=now + timedelta(minutes=5),
                details_json=json.dumps(details),
            )
        )

    await asyncio.sleep(0.4)
    assert await approval_repository.consume_approved_for_resume(
        approval_id=approval_id,
        owner_operator_session_id="operator-session-resume-attachment",
        operator_principal_id="operator:resume-attachment",
        job_id="job-resume-attachment",
        owner_kind="operator",
        owner_principal_id="operator:resume-attachment",
        service_id="service:resume-attachment",
        authority_digest="authority-resume-attachment",
        goal_id="goal-resume-attachment",
        goal_revision=1,
        plan_revision=1,
        capability_version="capability-resume-attachment-v1",
        budget_digest="budget-resume-attachment",
        expires_at=approval_expires_at,
    ) is None
    async with get_session() as db:
        row = (
            await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
        ).scalar_one()
        stored_details = json.loads(row.details_json or "{}")
        assert row.status == "expired"
        assert row.attachment_refs_json == "[]"
        assert stored_details["attachment_refs"] == []
        assert stored_details["attachment_refs_status"] == "expired"


@pytest.mark.asyncio
async def test_expired_outbox_attachment_is_cancelled_before_claim(file_db):
    get_session, _ = file_db
    await _add_owner(
        get_session,
        session_id="conversation-attachment-expiry",
        owner_id="operator:attachment",
        operator_session_id="operator-session-attachment",
    )
    now = datetime.now(timezone.utc)
    receipt = issue_attachment_quarantine_receipt(
        attachment_id="outbox-expiring",
        owner_principal_id="operator:attachment",
        content_hash="sha256:outbox-expiring",
        media_type="text/plain",
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(milliseconds=500),
    )
    queue = NativeNotificationQueue(lease_seconds=5, ttl_seconds=60)
    notification = await queue.enqueue(
        intervention_id="intervention-attachment-expiry",
        title="Expiring attachment",
        body="Must not dispatch",
        intervention_type="alert",
        urgency=4,
        session_id="conversation-attachment-expiry",
        owner_principal_id="operator:attachment",
        operator_session_id="operator-session-attachment",
        idempotency_key="attachment-expiry-notification",
        attachment_refs=[{
            "attachment_id": "outbox-expiring",
            "content_hash": "sha256:outbox-expiring",
            "media_type": "text/plain",
            "quarantine_status": "quarantined",
            "quarantine_receipt": receipt,
        }],
    )
    await asyncio.sleep(0.8)
    assert await queue.claim_next(worker_id="expired-attachment-worker") is None
    stored = await queue.get(notification.id)
    assert stored is not None
    assert stored.delivery_status == "cancelled"
    assert stored.degraded_state == "attachment_receipt_expired"
    assert stored.attachment_refs == []


@pytest.mark.asyncio
async def test_file_outbox_replay_is_idempotent_and_keeps_canonical_lineage(file_db):
    get_session, database_path = file_db
    await _add_owner(
        get_session,
        session_id="conversation-750",
        owner_id="operator:750",
        operator_session_id="operator-session-750",
    )
    queue = NativeNotificationQueue(max_attempts=2, lease_seconds=5, ttl_seconds=60)
    kwargs = dict(
        intervention_id="intervention-750",
        title="A durable update",
        body="Continue this thread",
        intervention_type="alert",
        urgency=4,
        session_id="conversation-750",
        thread_id="conversation-750",
        owner_principal_id="operator:750",
        operator_session_id="operator-session-750",
        correlation_id="corr-750",
        causation_id="message-750",
        idempotency_key="conversation-750:notification-1",
        attachment_refs=[
            _attachment_ref(
                attachment_id="attachment-1",
                owner_principal_id="operator:750",
                media_type="text/plain",
                content_hash="sha256:attachment-1",
                file_path="/private/secret.txt",
                token="secret-token",
            )
        ],
    )
    first = await queue.enqueue(**kwargs)
    second = await NativeNotificationQueue(max_attempts=2, lease_seconds=5, ttl_seconds=60).enqueue(**kwargs)

    assert first.id == second.id
    assert first.conversation_id == first.thread_id == first.session_id == "conversation-750"
    assert first.owner_principal_id == "operator:750"
    assert first.attachment_refs[0]["attachment_id"] == "attachment-1"
    assert first.attachment_refs[0]["owner_principal_id"] == "operator:750"
    assert first.attachment_refs[0]["quarantine_receipt_digest"]
    assert database_path.exists()
    async with get_session() as db:
        row = (await db.execute(select(NativeNotificationOutbox).where(NativeNotificationOutbox.id == first.id))).scalar_one()
        assert row.conversation_id == row.session_id == "conversation-750"
        assert row.thread_id == "conversation-750"
        assert "file_path" not in row.attachment_refs_json
        assert "secret-token" not in row.attachment_refs_json


@pytest.mark.asyncio
async def test_file_outbox_browser_scope_rejects_cross_owner_reads_and_dismissals(file_db):
    get_session, _ = file_db
    await _add_owner(
        get_session,
        session_id="conversation-owner-a",
        owner_id="operator:a",
        operator_session_id="operator-session-a",
    )
    await _add_owner(
        get_session,
        session_id="conversation-owner-b",
        owner_id="operator:b",
        operator_session_id="operator-session-b",
    )
    queue = NativeNotificationQueue(lease_seconds=5, ttl_seconds=60)
    notification_a = await queue.enqueue(
        intervention_id="intervention-owner-a",
        title="Owner A",
        body="Private A",
        intervention_type="alert",
        urgency=3,
        session_id="conversation-owner-a",
        owner_principal_id="operator:a",
        operator_session_id="operator-session-a",
        idempotency_key="owner-a-notification",
    )
    notification_b = await queue.enqueue(
        intervention_id="intervention-owner-b",
        title="Owner B",
        body="Private B",
        intervention_type="alert",
        urgency=3,
        session_id="conversation-owner-b",
        owner_principal_id="operator:b",
        operator_session_id="operator-session-b",
        idempotency_key="owner-b-notification",
    )
    ambient = await queue.enqueue(
        intervention_id=None,
        title="Ambient",
        body="Public local state",
        intervention_type="test",
        urgency=1,
        idempotency_key="ambient-notification",
    )

    owner_a_rows = await queue.list(
        owner_principal_id="operator:a",
        operator_session_id="operator-session-a",
    )
    assert {row.id for row in owner_a_rows} == {notification_a.id, ambient.id}
    assert await queue.get(
        notification_b.id,
        owner_principal_id="operator:a",
        operator_session_id="operator-session-a",
    ) is None
    assert await queue.dismiss(
        notification_b.id,
        owner_principal_id="operator:a",
        operator_session_id="operator-session-a",
    ) is None


@pytest.mark.asyncio
async def test_revoked_operator_session_cancels_before_dispatch_and_is_recoverable(file_db):
    get_session, _ = file_db
    revoked_at = datetime.now(timezone.utc)
    await _add_owner(
        get_session,
        session_id="conversation-revoked",
        owner_id="operator:revoked",
        operator_session_id="operator-session-revoked",
        revoked_at=revoked_at,
    )
    queue = NativeNotificationQueue(lease_seconds=5, ttl_seconds=60)
    notification = await queue.enqueue(
        intervention_id="intervention-revoked",
        title="Revoked update",
        body="Must not dispatch",
        intervention_type="alert",
        urgency=5,
        session_id="conversation-revoked",
        owner_principal_id="operator:revoked",
        operator_session_id="operator-session-revoked",
        idempotency_key="conversation-revoked:notification-1",
    )

    assert await queue.claim_next(worker_id="daemon-revoked") is None
    stored = await queue.get(notification.id)
    assert stored is not None
    assert stored.delivery_status == "cancelled"
    assert stored.degraded_state == "operator_session_revoked"
    recoveries = await queue.recovery()
    assert any(item["notification"]["id"] == notification.id for item in recoveries)


@pytest.mark.asyncio
async def test_restart_preserves_claim_attempt_and_unknown_receipt(file_db):
    get_session, _ = file_db
    await _add_owner(
        get_session,
        session_id="conversation-restart",
        owner_id="operator:restart",
        operator_session_id="operator-session-restart",
    )
    queue = NativeNotificationQueue(lease_seconds=1, ttl_seconds=60)
    notification = await queue.enqueue(
        intervention_id="intervention-restart",
        title="Restart update",
        body="Preserve this receipt",
        intervention_type="alert",
        urgency=3,
        session_id="conversation-restart",
        owner_principal_id="operator:restart",
        operator_session_id="operator-session-restart",
        idempotency_key="conversation-restart:notification-1",
    )
    claimed = await queue.claim_next(worker_id="daemon-before-restart")
    assert claimed is not None
    assert claimed.attempt_count == 1

    # A fresh queue object represents the restarted daemon.  The expired lease
    # is reconciled from the file-backed outbox and must become unknown rather
    # than silently replaying an ambiguous OS handoff.
    async with get_session() as db:
        row = (await db.execute(select(NativeNotificationOutbox).where(NativeNotificationOutbox.id == notification.id))).scalar_one()
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    restarted = NativeNotificationQueue(lease_seconds=1, ttl_seconds=60)
    recovered = await restarted.get(notification.id)
    assert recovered is not None
    assert recovered.delivery_status == "unknown"
    assert recovered.degraded_state == "delivery_unknown"
    assert recovered.attempt_count == 1
    attempts = await restarted.get_attempts(notification.id)
    assert attempts[0]["attempt_index"] == 1
    assert attempts[0]["status"] == "unknown"
    assert await restarted.claim_next(worker_id="daemon-after-restart") is None


def test_subprocess_restart_reuses_one_file_outbox_row(tmp_path):
    """Prove duplicate enqueue/recovery across process boundaries."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import sys

        from src.db.engine import init_db
        from src.observer.native_notification_queue import NativeNotificationQueue

        async def main():
            await init_db()
            queue = NativeNotificationQueue(lease_seconds=30, ttl_seconds=60)
            kwargs = dict(
                intervention_id="subprocess-intervention-750",
                title="Subprocess update",
                body="Durable duplicate",
                intervention_type="alert",
                urgency=3,
                idempotency_key="subprocess-conversation-750:notification-1",
            )
            first = await queue.enqueue(**kwargs)
            duplicate = await queue.enqueue(**kwargs)
            payload = {"first_id": first.id, "duplicate_id": duplicate.id}
            if len(sys.argv) > 1 and sys.argv[1] == "claim":
                claimed = await queue.claim_next(worker_id="subprocess-daemon")
                payload["claimed_id"] = claimed.id if claimed else None
                payload["attempt_count"] = claimed.attempt_count if claimed else None
            print(json.dumps(payload, sort_keys=True))

        asyncio.run(main())
        """
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            "WORKSPACE_DIR": str(workspace),
            "OPENROUTER_API_KEY": "test-key",
            "SERAPH_EXTERNAL_INFERENCE_DISABLED": "1",
        }
    )
    command = [sys.executable, "-c", script]
    first_run = subprocess.run(
        command,
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    second_run = subprocess.run(
        [*command, "claim"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    first_payload = json.loads(first_run.stdout.strip().splitlines()[-1])
    second_payload = json.loads(second_run.stdout.strip().splitlines()[-1])
    assert first_payload["first_id"] == first_payload["duplicate_id"]
    assert second_payload["first_id"] == first_payload["first_id"]
    assert second_payload["duplicate_id"] == first_payload["first_id"]
    assert second_payload["claimed_id"] == first_payload["first_id"]
    assert second_payload["attempt_count"] == 1
