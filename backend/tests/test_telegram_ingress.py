"""Focused proof for the provider-free #752 Telegram ingress contract."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from src.extensions.telegram_ingress import (
    OPENROUTER_INFERENCE_CONSENT_SCOPE,
    TELEGRAM_TRANSIT_CONSENT_SCOPE,
    TelegramAttachmentMetadata,
    TelegramConsent,
    TelegramConsentState,
    TelegramIngressPolicy,
    TelegramIngressResult,
    TelegramIngressState,
    TelegramIngressStatus,
    TelegramPairingLifecycleState,
    TelegramPairingSnapshot,
    TelegramUpdate,
    canonical_telegram_idempotency_key,
    canonical_telegram_request_digest,
    ingest_telegram_update,
    serialize_telegram_receipt,
    validate_telegram_update,
)
from src.guardian.audio_ingress import AudioProviderStatus


NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _consent(
    reference: str,
    *,
    state: TelegramConsentState = TelegramConsentState.ACTIVE,
    granted_at: datetime = NOW - timedelta(days=1),
    expires_at: datetime = NOW + timedelta(days=1),
    scope: str = TELEGRAM_TRANSIT_CONSENT_SCOPE,
) -> TelegramConsent:
    return TelegramConsent(reference, state, granted_at, expires_at, scope)


def _policy(**changes) -> TelegramIngressPolicy:
    changes.setdefault(
        "pairing_snapshot",
        TelegramPairingSnapshot(
            pairing_id="telegram-pairing-1",
            operator_id=42,
            chat_id=9001,
            authority_reference="pairing-authority:1",
        ),
    )
    return TelegramIngressPolicy(operator_id=42, chat_id=9001, **changes)


def _update(**changes) -> TelegramUpdate:
    update = TelegramUpdate(
        operator_id=42,
        chat_id=9001,
        update_id=1,
        message_id=101,
        received_at=NOW,
        text="  secret operator message  ",
        external_transit_consent=_consent("consent:telegram:1"),
        openrouter_consent=_consent(
            "consent:openrouter:1", scope=OPENROUTER_INFERENCE_CONSENT_SCOPE
        ),
        sequence=1,
    )
    return replace(update, **changes)


def _voice_update(**changes) -> TelegramUpdate:
    return _update(
        text=None,
        attachment=TelegramAttachmentMetadata(
            attachment_id="voice-1",
            media_type="audio/ogg",
            size_bytes=128,
            content_hash="sha256:" + "a" * 64,
            duration_seconds=2.5,
            file_reference="telegram-file-token-secret",
        ),
        **changes,
    )


def test_text_accepts_with_server_owned_identity_and_redacted_receipt():
    policy = _policy(provider_status=AudioProviderStatus.UNVERIFIED)
    update = _update()
    result = validate_telegram_update(update, policy=policy, now=NOW)

    assert result.status is TelegramIngressStatus.ACCEPTED
    assert result.accepted is True
    assert result.reason_code == "text_ingress_accepted_provider_unverified"
    assert result.request_digest == canonical_telegram_request_digest(update)
    assert result.idempotency_key == canonical_telegram_idempotency_key(update)

    payload = serialize_telegram_receipt(update, result, policy=policy).as_payload()
    encoded = json.dumps(payload, sort_keys=True)
    assert payload["status"] == "accepted"
    assert payload["identity"] == {
        "operator_id": 42,
        "chat_id": 9001,
        "update_id": 1,
        "message_id": 101,
        "sequence": 1,
    }
    assert payload["content"]["redacted"] is True
    assert "secret operator message" not in encoded
    assert payload["provider"]["model_dispatch_claimed"] is False
    assert payload["provider"]["local_fallback_claimed"] is False


def test_identity_fallback_and_blank_text_fail_closed():
    policy = _policy()
    cases = (
        (_update(operator_id=43), "telegram_identity_not_allowlisted"),
        (_update(chat_id=9002), "telegram_identity_not_allowlisted"),
        (_update(server_owned_identity=False), "server_owned_identity_required"),
        (_update(local_fallback_requested=True), "local_fallback_forbidden"),
        (_update(text="   "), "text_required"),
    )
    for update, reason in cases:
        result = validate_telegram_update(update, policy=policy, now=NOW)
        assert result.status is TelegramIngressStatus.BLOCKED
        assert result.reason_code == reason


def test_pairing_snapshot_is_required_and_lifecycle_bound():
    update = _update()
    missing = validate_telegram_update(
        update,
        policy=_policy(pairing_snapshot=None),
        now=NOW,
    )
    mismatched = validate_telegram_update(
        update,
        policy=_policy(
            pairing_snapshot=TelegramPairingSnapshot(
                pairing_id="telegram-pairing-other",
                operator_id=42,
                chat_id=9001,
                authority_reference="pairing-authority:1",
            )
        ),
        now=NOW,
    )
    revoked = validate_telegram_update(
        update,
        policy=_policy(
            pairing_snapshot=TelegramPairingSnapshot(
                pairing_id="telegram-pairing-1",
                operator_id=42,
                chat_id=9001,
                lifecycle=TelegramPairingLifecycleState.REVOKED,
                authority_reference="pairing-authority:1",
            )
        ),
        now=NOW,
    )
    expired = validate_telegram_update(
        update,
        policy=_policy(
            pairing_snapshot=TelegramPairingSnapshot(
                pairing_id="telegram-pairing-1",
                operator_id=42,
                chat_id=9001,
                expires_at=NOW - timedelta(seconds=1),
                authority_reference="pairing-authority:1",
            )
        ),
        now=NOW,
    )

    assert missing.reason_code == "pairing_snapshot_required"
    assert mismatched.reason_code == "pairing_identity_mismatch"
    assert revoked.reason_code == "pairing_revoked"
    assert expired.reason_code == "pairing_expired"
    assert all(item.status is TelegramIngressStatus.BLOCKED for item in (missing, mismatched, revoked, expired))


def test_consent_boundaries_are_required_current_and_separate():
    policy = _policy()
    cases = (
        (_update(external_transit_consent=None), "consent_missing"),
        (_update(openrouter_consent=None), "consent_missing"),
        (
            _update(
                openrouter_consent=_consent(
                    "consent:telegram:1", scope=OPENROUTER_INFERENCE_CONSENT_SCOPE
                )
            ),
            "consent_references_not_separate",
        ),
        (_update(external_transit_consent=_consent("consent:telegram:revoked", state=TelegramConsentState.REVOKED)), "consent_revoked"),
        (
            _update(
                external_transit_consent=_consent(
                    "consent:wrong-scope", scope=OPENROUTER_INFERENCE_CONSENT_SCOPE
                )
            ),
            "consent_scope_invalid",
        ),
        (
            _update(
                openrouter_consent=_consent(
                    "consent:openrouter:expired",
                    expires_at=NOW - timedelta(seconds=1),
                    scope=OPENROUTER_INFERENCE_CONSENT_SCOPE,
                )
            ),
            "consent_expired",
        ),
    )
    for update, reason in cases:
        result = validate_telegram_update(update, policy=policy, now=NOW)
        assert result.status is TelegramIngressStatus.BLOCKED
        assert result.reason_code == reason


def test_ingest_advances_sequence_and_rejects_duplicate_conflict_and_replay():
    policy = _policy()
    update = _update()
    first, state = ingest_telegram_update(TelegramIngressState(), update, policy, now=NOW)
    duplicate = validate_telegram_update(update, state, policy, now=NOW)
    duplicate_with_new_arrival_time = validate_telegram_update(
        replace(update, received_at=NOW + timedelta(seconds=5)), state, policy, now=NOW
    )
    conflict = validate_telegram_update(replace(update, text="different"), state, policy, now=NOW)
    old_sequence = validate_telegram_update(
        replace(update, update_id=2, message_id=102, sequence=1), state, policy, now=NOW
    )

    assert first.status is TelegramIngressStatus.ACCEPTED
    assert state.last_sequence == 1
    assert duplicate.status is TelegramIngressStatus.DUPLICATE
    assert duplicate.reason_code == "request_already_recorded"
    assert duplicate_with_new_arrival_time.status is TelegramIngressStatus.DUPLICATE
    assert canonical_telegram_request_digest(update) == canonical_telegram_request_digest(
        replace(update, received_at=NOW + timedelta(seconds=5))
    )
    assert conflict.reason_code == "replay_conflict"
    assert old_sequence.reason_code == "update_not_monotonic"


def test_replay_rechecks_revoked_and_expired_consent_before_duplicate_return():
    policy = _policy()
    update = _update()
    _, state = ingest_telegram_update(TelegramIngressState(), update, policy, now=NOW)

    revoked = replace(
        update,
        external_transit_consent=replace(
            update.external_transit_consent,
            state=TelegramConsentState.REVOKED,
        ),
    )
    expired = replace(
        update,
        openrouter_consent=replace(
            update.openrouter_consent,
            expires_at=NOW - timedelta(seconds=1),
        ),
    )

    revoked_retry = validate_telegram_update(revoked, state, policy, now=NOW)
    expired_retry = validate_telegram_update(expired, state, policy, now=NOW)

    assert revoked_retry.status is TelegramIngressStatus.BLOCKED
    assert revoked_retry.reason_code == "consent_revoked"
    assert expired_retry.status is TelegramIngressStatus.BLOCKED
    assert expired_retry.reason_code == "consent_expired"


def test_receipt_rechecks_consent_after_acceptance_is_revoked():
    policy = _policy()
    update = _update()
    accepted = validate_telegram_update(update, policy=policy, now=NOW)
    revoked = replace(
        update,
        external_transit_consent=replace(
            update.external_transit_consent,
            state=TelegramConsentState.REVOKED,
        ),
    )

    payload = serialize_telegram_receipt(revoked, accepted, policy=policy).as_payload()

    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "consent_revoked"
    assert payload["request_digest"] is None
    assert payload["identity"]["operator_id"] is None
    assert payload["consent"]["external_transit_reference"] is None


def test_receipt_rechecks_consent_after_acceptance_expires():
    policy = _policy()
    update = _update()
    accepted = validate_telegram_update(update, policy=policy, now=NOW)
    expired = replace(
        update,
        openrouter_consent=replace(
            update.openrouter_consent,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
    )

    payload = serialize_telegram_receipt(expired, accepted, policy=policy).as_payload()

    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "consent_expired"
    assert payload["request_digest"] is None
    assert payload["identity"]["operator_id"] is None


def test_voice_is_quarantined_and_handoff_is_metadata_only():
    policy = _policy(
        provider_status=AudioProviderStatus.READY,
        trusted_adapter_id="audio-adapter-1",
        provider_proof_reference="provider-proof:1",
        consent_proof_reference="consent-proof:1",
    )
    update = _voice_update()
    result = validate_telegram_update(update, policy=policy, now=NOW)
    payload = serialize_telegram_receipt(update, result, policy=policy).as_payload()
    encoded = json.dumps(payload, sort_keys=True)

    assert result.status is TelegramIngressStatus.DEGRADED
    assert result.reason_code == "voice_ingress_degraded_preflight_proof_required"
    assert result.attachment_quarantined is True
    assert result.voice_handoff is not None
    assert result.voice_handoff.decode_claimed is False
    assert result.voice_handoff.send_claimed is False
    assert payload["attachment"]["quarantine_status"] == "quarantined"
    assert payload["attachment"]["file_reference"] is None
    assert "telegram-file-token-secret" not in encoded
    assert "audio_payload" not in encoded
    assert payload["consent"]["external_transit_scope"] == TELEGRAM_TRANSIT_CONSENT_SCOPE
    assert payload["consent"]["openrouter_scope"] == OPENROUTER_INFERENCE_CONSENT_SCOPE
    assert payload["provider"]["preflight_proof_present"] is False
    assert payload["voice_handoff"]["decode_claimed"] is False
    assert payload["voice_handoff"]["send_claimed"] is False


def test_voice_degrades_without_provider_and_text_remains_usable():
    unavailable = _policy(provider_status=AudioProviderStatus.UNAVAILABLE)
    unverified = _policy(provider_status=AudioProviderStatus.UNVERIFIED)
    voice = _voice_update()

    unavailable_voice = validate_telegram_update(voice, policy=unavailable, now=NOW)
    unverified_voice = validate_telegram_update(voice, policy=unverified, now=NOW)
    unavailable_text = validate_telegram_update(_update(), policy=unavailable, now=NOW)

    assert unavailable_voice.status is TelegramIngressStatus.DEGRADED
    assert unverified_voice.status is TelegramIngressStatus.DEGRADED
    assert unavailable_voice.retryable is True
    assert unavailable_text.status is TelegramIngressStatus.ACCEPTED
    assert unavailable_text.accepted is True


def test_ready_voice_requires_existing_audio_preflight_proof():
    update = _voice_update()
    result = validate_telegram_update(
        update,
        policy=_policy(provider_status=AudioProviderStatus.READY),
        now=NOW,
    )
    payload = serialize_telegram_receipt(
        update, result, policy=_policy(provider_status=AudioProviderStatus.READY)
    ).as_payload()

    assert result.status is TelegramIngressStatus.DEGRADED
    assert result.reason_code == "voice_ingress_degraded_preflight_proof_required"
    assert result.voice_handoff is not None
    assert result.voice_handoff.decode_claimed is False
    assert payload["provider"]["preflight_proof_present"] is False


def test_degraded_voice_is_recorded_once_and_distinct_updates_remain_retryable():
    policy = _policy(provider_status=AudioProviderStatus.UNAVAILABLE)
    voice = _voice_update()
    first, state = ingest_telegram_update(TelegramIngressState(), voice, policy, now=NOW)
    retry = validate_telegram_update(voice, state, policy, now=NOW)
    distinct = validate_telegram_update(
        replace(voice, update_id=2, message_id=102, sequence=2), state, policy, now=NOW
    )

    assert first.status is TelegramIngressStatus.DEGRADED
    assert state.last_sequence == 1
    assert len(state.replay_entries) == 1
    assert retry.status is TelegramIngressStatus.DUPLICATE
    assert distinct.status is TelegramIngressStatus.DEGRADED


@pytest.mark.parametrize(
    ("attachment", "reason"),
    (
        (TelegramAttachmentMetadata("image-1", "image/png", 10, "a" * 64, duration_seconds=1), "attachment_media_type_not_allowed"),
        (TelegramAttachmentMetadata("voice-1", "audio/ogg", 11, "a" * 64, duration_seconds=1), "attachment_size_exceeds_limit"),
        (TelegramAttachmentMetadata("voice-1", "audio/ogg", 10, "bad", duration_seconds=1), "attachment_hash_invalid"),
        (TelegramAttachmentMetadata("voice-1", "audio/ogg", 10, "a" * 64, duration_seconds=1), "attachment_required_voice"),
    ),
)
def test_attachment_limits_and_text_attachment_conflict_fail_closed(attachment, reason):
    policy = _policy(max_attachment_bytes=10)
    update = _update(attachment=attachment, text="text" if reason == "attachment_required_voice" else None)
    result = validate_telegram_update(update, policy=policy, now=NOW)
    assert result.status is TelegramIngressStatus.BLOCKED
    assert result.reason_code == reason
    assert result.attachment_quarantined is True


def test_freshness_sequence_and_rate_limits_are_bounded():
    policy = _policy(max_age_seconds=30, max_clock_skew_seconds=5, rate_limit_max_updates=1)
    assert validate_telegram_update(
        _update(received_at=NOW - timedelta(seconds=31)), policy=policy, now=NOW
    ).reason_code == "update_too_old"
    assert validate_telegram_update(
        _update(received_at=NOW + timedelta(seconds=6)), policy=policy, now=NOW
    ).reason_code == "update_clock_ahead"
    first, state = ingest_telegram_update(TelegramIngressState(), _update(), policy, now=NOW)
    second = validate_telegram_update(
        _update(update_id=2, message_id=102, sequence=2), state, policy, now=NOW
    )
    assert first.status is TelegramIngressStatus.ACCEPTED
    assert second.reason_code == "rate_limit_exceeded"


def test_receipt_rejects_forged_result_and_redacts_blocked_metadata():
    policy = _policy()
    update = _update()
    forged = TelegramIngressResult(
        status=TelegramIngressStatus.ACCEPTED,
        reason_code="text_ingress_accepted",
        accepted=True,
        retryable=False,
        request_digest=canonical_telegram_request_digest(update),
        idempotency_key=canonical_telegram_idempotency_key(update),
    )
    payload = serialize_telegram_receipt(update, forged, policy=policy).as_payload()

    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_result_provenance"
    assert payload["request_digest"] is None
    assert payload["identity"]["operator_id"] is None
    assert "secret operator message" not in json.dumps(payload, sort_keys=True)

    admitted = validate_telegram_update(update, policy=policy, now=NOW)
    mismatched = serialize_telegram_receipt(
        update,
        admitted,
        policy=_policy(max_text_bytes=2048),
    ).as_payload()
    assert mismatched["status"] == "blocked"
    assert mismatched["reason_code"] == "invalid_result_provenance"


def test_receipt_serialization_blocks_malformed_attachment_without_leaking_identity():
    policy = _policy()
    update = _update()
    admitted = validate_telegram_update(update, policy=policy, now=NOW)
    malformed = replace(update, attachment="not-metadata")

    payload = serialize_telegram_receipt(malformed, admitted, policy=policy).as_payload()

    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_attachment"
    assert payload["request_digest"] is None
    assert payload["identity"]["operator_id"] is None


def test_malformed_clock_state_and_expired_voice_retention_fail_closed():
    policy = _policy(max_age_seconds=60, max_raw_retention_seconds=30)
    naive_clock = validate_telegram_update(
        _update(), policy=policy, now=NOW.replace(tzinfo=None)
    )
    malformed_state = validate_telegram_update(
        _update(),
        TelegramIngressState(rate_events=("not-an-event",)),
        policy,
        now=NOW,
    )
    expired_voice = validate_telegram_update(
        _voice_update(received_at=NOW - timedelta(seconds=31)),
        policy=policy,
        now=NOW,
    )

    assert naive_clock.reason_code == "invalid_timestamp"
    assert malformed_state.reason_code == "invalid_pairing_state"
    assert expired_voice.reason_code == "attachment_retention_expired"
    assert expired_voice.attachment_quarantined is True


def test_dataclasses_are_immutable_and_state_is_caller_owned():
    update = _update()
    with pytest.raises(FrozenInstanceError):
        update.sequence = 2  # type: ignore[misc]
    state = TelegramIngressState()
    _, next_state = ingest_telegram_update(state, update, _policy(), now=NOW)
    assert state.last_sequence == 0
    assert next_state.last_sequence == 1
