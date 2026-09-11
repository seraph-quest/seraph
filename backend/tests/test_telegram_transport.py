"""Focused local proof for the provider-free Telegram transport seam."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import patch

import pytest

from config.settings import settings
from src.extensions.telegram_transport import (
    RecordingTelegramTransport,
    TelegramTransportAdapter,
    TelegramTransportError,
)
from src.extensions.telegram_ingress import (
    TelegramConsent,
    TelegramConsentState,
    TelegramIngressState,
    TelegramUpdate,
    ingest_telegram_update,
)
from src.db.models import TelegramTransportState


def _update(update_id: int = 1, *, operator_id: int = 42, chat_id: int = 77, sequence: int | None = None):
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": operator_id},
            "chat": {"id": chat_id},
            "text": f"hello {update_id}",
        },
    }
    if sequence is not None:
        payload["sequence"] = sequence
    return payload


def test_adapter_policy_uses_a_server_owned_authority_reference():
    """The adapter policy must reach ingress validation, not invalid_policy."""
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    row = TelegramTransportState(
        id="telegram",
        pairing_id="telegram-pairing:test",
        pairing_state="active",
        operator_id=42,
        chat_id=77,
        pairing_expires_at=now + timedelta(hours=1),
        transit_consent_reference="telegram-consent:transit:test",
        transit_consent_expires_at=now + timedelta(minutes=10),
        model_consent_reference="telegram-consent:model:test",
        model_consent_expires_at=now + timedelta(minutes=10),
    )
    adapter = TelegramTransportAdapter()
    policy = adapter._policy(row, now=now)
    consent = TelegramConsent(
        reference="telegram-consent:transit:test",
        state=TelegramConsentState.ACTIVE,
        granted_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
        scope="telegram_transit",
    )
    model_consent = TelegramConsent(
        reference="telegram-consent:model:test",
        state=TelegramConsentState.ACTIVE,
        granted_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
        scope="openrouter_inference",
    )
    update = TelegramUpdate(
        operator_id=42,
        chat_id=77,
        update_id=1,
        message_id=101,
        received_at=now,
        text="hello",
        external_transit_consent=consent,
        openrouter_consent=model_consent,
    )
    accepted, _ = ingest_telegram_update(TelegramIngressState(), update, policy, now=now)
    assert accepted.status.value == "accepted"
    blocked, _ = ingest_telegram_update(
        TelegramIngressState(),
        replace(update, chat_id=78),
        policy,
        now=now,
    )
    assert blocked.reason_code == "telegram_identity_not_allowlisted"


@pytest.mark.asyncio
async def test_text_ingress_is_durable_and_duplicate_after_restart(async_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_secret", "telegram-test-server-secret")
    transport = RecordingTelegramTransport()
    adapter = TelegramTransportAdapter(transport=transport)
    await adapter.pair(
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        operator_id=42,
        chat_id=77,
        token="synthetic-secret",
    )
    await adapter.grant_consent(
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        boundary="telegram_transit",
    )
    await adapter.grant_consent(
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        boundary="openrouter_inference",
    )

    first = await adapter.ingest_update(
        _update(sequence=1),
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
    )
    assert first["status"] == "accepted"
    assert first["canonical_channel"] == "telegram"
    assert first["session_id"]
    assert first["canonical_message_id"]
    assert "hello 1" not in first["ingress_receipt"]

    restarted = TelegramTransportAdapter(transport=transport)
    duplicate = await restarted.ingest_update(
        _update(sequence=1),
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
    )
    assert duplicate["idempotency_key"] == first["idempotency_key"]
    assert duplicate["canonical_message_id"] == first["canonical_message_id"]


@pytest.mark.asyncio
async def test_cross_owner_and_chat_are_denied_before_ingress(async_db):
    adapter = TelegramTransportAdapter()
    await adapter.pair(
        owner_principal_id="operator:owner-a",
        operator_session_id="operator-session-a",
        operator_id=42,
        chat_id=77,
    )
    await adapter.grant_consent(
        owner_principal_id="operator:owner-a",
        operator_session_id="operator-session-a",
        boundary="telegram_transit",
    )
    await adapter.grant_consent(
        owner_principal_id="operator:owner-a",
        operator_session_id="operator-session-a",
        boundary="openrouter_inference",
    )
    with pytest.raises(TelegramTransportError, match="another operator"):
        await adapter.ingest_update(
            _update(sequence=1),
            owner_principal_id="operator:owner-b",
            operator_session_id="operator-session-b",
        )
    blocked = await adapter.ingest_update(
        _update(sequence=1, chat_id=78),
        owner_principal_id="operator:owner-a",
        operator_session_id="operator-session-a",
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "telegram_identity_not_allowlisted"


@pytest.mark.asyncio
async def test_voice_is_quarantined_and_delivery_rechecks_revoke(async_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_secret", "telegram-test-server-secret")
    transport = RecordingTelegramTransport()
    adapter = TelegramTransportAdapter(transport=transport)
    await adapter.pair(
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        operator_id=42,
        chat_id=77,
    )
    for boundary in ("telegram_transit", "openrouter_inference"):
        await adapter.grant_consent(
            owner_principal_id="operator:test",
            operator_session_id="operator-session-1",
            boundary=boundary,
        )
    result = await adapter.ingest_update(
        {
            "update_id": 2,
            "sequence": 1,
            "message": {
                "message_id": 102,
                "from": {"id": 42},
                "chat": {"id": 77},
                "voice": {
                    "file_unique_id": "voice-2",
                    "media_type": "audio/ogg",
                    "size_bytes": 128,
                    "content_hash": "sha256:" + "a" * 64,
                    "duration_seconds": 2.0,
                },
            },
        },
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
    )
    assert result["status"] == "degraded"
    assert result["attachment_quarantine"] == "quarantined"
    assert result["voice_handoff"]["status"] != "none"

    outbox = await adapter.enqueue_outbound(
        "reply",
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        idempotency_key="telegram-outbound-1",
    )
    await adapter.revoke(owner_principal_id="operator:test", operator_session_id="operator-session-1")
    delivered = await adapter.deliver(
        outbox["id"],
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
    )
    assert delivered["status"] == "cancelled"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_outbox_retry_and_idempotency_conflict(async_db):
    transport = RecordingTelegramTransport(responses=[{"status_code": 503}, {"status_code": 200, "message_id": "m-1"}])
    adapter = TelegramTransportAdapter(transport=transport)
    await adapter.pair(
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        operator_id=42,
        chat_id=77,
    )
    await adapter.grant_consent(
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        boundary="telegram_transit",
    )
    outbox = await adapter.enqueue_outbound(
        "reply",
        owner_principal_id="operator:test",
        operator_session_id="operator-session-1",
        idempotency_key="telegram-outbound-1",
    )
    retry = await adapter.deliver(outbox["id"], owner_principal_id="operator:test", operator_session_id="operator-session-1")
    assert retry["status"] == "queued"
    done = await adapter.deliver(outbox["id"], owner_principal_id="operator:test", operator_session_id="operator-session-1")
    assert done["status"] == "delivered"
    with pytest.raises(TelegramTransportError, match="another payload"):
        await adapter.enqueue_outbound(
            "changed",
            owner_principal_id="operator:test",
            operator_session_id="operator-session-1",
            idempotency_key="telegram-outbound-1",
        )
