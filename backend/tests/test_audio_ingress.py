"""Focused proof for the provider-free #751 PTT audio preflight contract."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from src.guardian.audio_ingress import (
    AUDIO_INGRESS_RECEIPT_SCHEMA_VERSION,
    AudioConsent,
    AudioConsentState,
    AudioFormatRule,
    AudioIngressPolicy,
    AudioIngressRequest,
    AudioIngressStatus,
    AudioProviderStatus,
    AudioRequestIdentity,
    build_openrouter_input_audio,
    canonical_audio_request_digest,
    serialize_audio_ingress_receipt,
    validate_audio_ingress,
)


NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
CAPTURE_REF = "consent:capture:ptt-1"
CLOUD_REF = "consent:cloud:ptt-1"


def _consent(
    reference: str,
    *,
    state: AudioConsentState = AudioConsentState.ACTIVE,
    expires_at: datetime = NOW + timedelta(minutes=10),
) -> AudioConsent:
    return AudioConsent(
        reference=reference,
        state=state,
        granted_at=NOW - timedelta(minutes=1),
        expires_at=expires_at,
    )


def _policy(*, provider_status: AudioProviderStatus = AudioProviderStatus.READY) -> AudioIngressPolicy:
    return AudioIngressPolicy(provider_status=provider_status)


def _request(**changes) -> AudioIngressRequest:
    request = AudioIngressRequest(
        session_id="session-1",
        message_id="message-1",
        attachment_id="attachment-1",
        request_id="audio-request-1",
        captured_at=NOW,
        audio_base64="YQ==",
        audio_size_bytes=1,
        duration_seconds=1.0,
        media_type="audio/wav",
        container="wav",
        codec="pcm_s16le",
        stream_count=1,
        normalized_wav_size_bytes=100,
        capture_consent=_consent(CAPTURE_REF),
        cloud_upload_consent=_consent(CLOUD_REF),
        raw_audio_retention_deadline=NOW + timedelta(minutes=10),
        requested_capability="chat",
    )
    return replace(request, **changes)


def test_request_is_immutable_and_digest_and_openrouter_shape_are_metadata_bound():
    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.audio_size_bytes = 2  # type: ignore[misc]

    assert canonical_audio_request_digest(request) == canonical_audio_request_digest(request)
    changed = _request(request_id="audio-request-2")
    assert canonical_audio_request_digest(request) != canonical_audio_request_digest(changed)
    assert build_openrouter_input_audio(request).as_payload() == {
        "type": "input_audio",
        "input_audio": {"data": "YQ==", "format": "wav"},
    }


def test_ready_openrouter_preflight_accepts_chat_and_receipt_never_contains_audio_or_transcript():
    request = _request()
    result = validate_audio_ingress(request, policy=_policy(), now=NOW)
    assert result.status is AudioIngressStatus.ACCEPTED
    assert result.accepted is True
    receipt = serialize_audio_ingress_receipt(request, result, policy=_policy())
    payload = receipt.as_payload()
    encoded = json.dumps(payload, sort_keys=True)
    assert receipt.schema_version == AUDIO_INGRESS_RECEIPT_SCHEMA_VERSION
    assert "YQ==" not in encoded
    assert "raw transcript" not in encoded.lower()
    assert payload["privacy"] == {"raw_audio_in_receipt": False, "transcript_in_receipt": False}
    assert payload["provider"]["live_call_claimed"] is False


def test_unverified_or_unavailable_provider_is_degraded_without_local_fallback():
    unverified = validate_audio_ingress(_request(), now=NOW)
    assert unverified.status is AudioIngressStatus.DEGRADED
    assert unverified.reason_code == "openrouter_capability_unverified"
    unavailable = validate_audio_ingress(
        _request(), policy=_policy(provider_status=AudioProviderStatus.UNAVAILABLE), now=NOW
    )
    assert unavailable.status is AudioIngressStatus.DEGRADED
    assert unavailable.reason_code == "openrouter_route_unavailable"
    local = validate_audio_ingress(_request(local_fallback_requested=True), policy=_policy(), now=NOW)
    assert local.status is AudioIngressStatus.BLOCKED
    assert local.reason_code == "local_fallback_forbidden"
    wrong_provider = validate_audio_ingress(_request(inference_provider="whisper"), policy=_policy(), now=NOW)
    assert wrong_provider.reason_code == "provider_not_openrouter"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"capture_consent": None}, "capture_consent_missing"),
        ({"cloud_upload_consent": None}, "cloud_upload_consent_missing"),
        ({"capture_consent": _consent(CAPTURE_REF, state=AudioConsentState.STALE)}, "capture_consent_stale"),
        ({"cloud_upload_consent": _consent(CLOUD_REF, state=AudioConsentState.REVOKED)}, "cloud_upload_consent_revoked"),
        ({"capture_consent": _consent(CAPTURE_REF, expires_at=NOW)}, "capture_consent_stale"),
        ({"capture_consent": _consent(CAPTURE_REF), "cloud_upload_consent": _consent(CAPTURE_REF)}, "consent_references_not_separate"),
    ],
)
def test_capture_and_cloud_consents_are_separate_current_and_revocable(changes, reason):
    result = validate_audio_ingress(_request(**changes), policy=_policy(), now=NOW)
    assert result.status is AudioIngressStatus.BLOCKED
    assert result.reason_code == reason


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"audio_size_bytes": 10 * 1024 * 1024 + 1}, "audio_size_exceeds_limit"),
        ({"duration_seconds": 60.1}, "duration_exceeds_limit"),
        ({"normalized_wav_size_bytes": 2 * 1024 * 1024 + 1}, "normalized_wav_size_exceeds_limit"),
        ({"stream_count": 2}, "multiple_streams_not_allowed"),
        ({"media_type": "audio/mp4", "container": "m4a", "codec": "opus"}, "audio_format_not_allowed"),
    ],
)
def test_audio_size_duration_normalization_stream_and_format_limits_fail_closed(changes, reason):
    result = validate_audio_ingress(_request(**changes), policy=_policy(), now=NOW)
    assert result.status is AudioIngressStatus.BLOCKED
    assert result.reason_code == reason


def test_raw_audio_retention_deadline_is_required_and_capped_at_fifteen_minutes():
    expired = validate_audio_ingress(
        _request(raw_audio_retention_deadline=NOW), policy=_policy(), now=NOW
    )
    assert expired.reason_code == "raw_audio_retention_expired"
    too_long = validate_audio_ingress(
        _request(raw_audio_retention_deadline=NOW + timedelta(minutes=15, seconds=1)),
        policy=_policy(),
        now=NOW,
    )
    assert too_long.reason_code == "raw_audio_retention_exceeds_limit"
    missing = validate_audio_ingress(_request(raw_audio_retention_deadline=None), policy=_policy(), now=NOW)
    assert missing.reason_code == "raw_audio_retention_deadline_missing"


def test_non_chat_capability_requires_confirmed_transcript_but_chat_does_not():
    analysis = validate_audio_ingress(
        _request(requested_capability="analysis"), policy=_policy(), now=NOW
    )
    assert analysis.reason_code == "transcript_confirmation_required"
    confirmed = validate_audio_ingress(
        _request(
            requested_capability="analysis",
            transcript_confirmed=True,
            transcript_confirmation_ref="confirm:transcript:1",
        ),
        policy=_policy(),
        now=NOW,
    )
    assert confirmed.status is AudioIngressStatus.ACCEPTED
    chat = validate_audio_ingress(
        _request(requested_capability="chat", transcript_confirmed=False), policy=_policy(), now=NOW
    )
    assert chat.status is AudioIngressStatus.ACCEPTED


def test_invalid_base64_data_uri_size_metadata_and_server_identity_are_blocked():
    for changes, reason in (
        ({"audio_base64": "data:audio/wav;base64,YQ=="}, "invalid_base64_payload"),
        ({"audio_base64": "YQ=", "audio_size_bytes": 1}, "invalid_base64_payload"),
        ({"audio_base64": "YQ==", "audio_size_bytes": 2}, "audio_size_metadata_mismatch"),
        ({"identity_server_owned": False}, "server_owned_identity_required"),
    ):
        result = validate_audio_ingress(_request(**changes), policy=_policy(), now=NOW)
        assert result.status is AudioIngressStatus.BLOCKED
        assert result.reason_code == reason


def test_duplicate_request_identity_is_idempotent_and_conflicts_fail_closed():
    request = _request()
    digest = canonical_audio_request_digest(request)
    known = (
        AudioRequestIdentity(
            request_id=request.request_id,
            session_id=request.session_id,
            message_id=request.message_id,
            attachment_id=request.attachment_id,
            request_digest=digest,
        ),
    )
    duplicate = validate_audio_ingress(request, policy=_policy(), known_identities=known, now=NOW)
    assert duplicate.status is AudioIngressStatus.DUPLICATE
    conflict = validate_audio_ingress(
        _request(duration_seconds=2.0), policy=_policy(), known_identities=known, now=NOW
    )
    assert conflict.status is AudioIngressStatus.BLOCKED
    assert conflict.reason_code == "request_identity_conflict"
    attachment_conflict = validate_audio_ingress(
        _request(request_id="audio-request-2"), policy=_policy(), known_identities=known, now=NOW
    )
    assert attachment_conflict.reason_code == "attachment_identity_conflict"


def test_consent_and_provider_metadata_are_not_a_local_model_or_network_claim():
    result = validate_audio_ingress(_request(), policy=_policy(), now=NOW)
    receipt = serialize_audio_ingress_receipt(_request(), result, policy=_policy()).as_payload()
    assert receipt["provider"]["name"] == "openrouter"
    assert receipt["provider"]["live_call_claimed"] is False
    assert receipt["provider"]["local_fallback_claimed"] is False
    assert receipt["consent"]["capture_reference"] == CAPTURE_REF
    assert receipt["consent"]["cloud_upload_reference"] == CLOUD_REF


def test_malformed_consent_and_policy_shapes_fail_closed_without_receipt_content_leak():
    malformed_consent = _request(capture_consent=object())
    result = validate_audio_ingress(malformed_consent, policy=_policy(), now=NOW)
    assert result.status is AudioIngressStatus.BLOCKED
    assert result.reason_code == "capture_consent_invalid"
    receipt = serialize_audio_ingress_receipt(malformed_consent, result, policy=_policy()).as_payload()
    assert receipt["consent"]["capture_reference"] is None
    invalid_policy = AudioIngressPolicy(
        allowed_formats=(AudioFormatRule("audio/wav", [], "pcm_s16le"),),  # type: ignore[arg-type]
        provider_status=AudioProviderStatus.READY,
    )
    policy_result = validate_audio_ingress(_request(), policy=invalid_policy, now=NOW)
    assert policy_result.status is AudioIngressStatus.BLOCKED
    assert policy_result.reason_code == "invalid_policy"
