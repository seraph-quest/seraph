"""Deterministic proof for the provider-free #749 node pairing contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.extensions.node_pairing import (
    DEFAULT_POLICY_VERSION,
    NodePairingPolicy,
    NodePairingRequest,
    NodePairingState,
    PairingIngressStatus,
    PairingLifecycleAction,
    PairingLifecycleState,
    apply_pairing_transition,
    canonical_request_digest,
    ingest_pairing_request,
    scoped_credential_fingerprint,
    serialize_pairing_receipt,
    serialize_transition_receipt,
    validate_pairing_request,
)
from src.extensions import paired_edge


NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
TOKEN = "test-only-credential"
DEVICE_ID = "mac-edge-1"
PAIRING_ID = "pairing-1"
SCOPE = '["mac-edge-1","pairing-1","presence.read","screen_capture"]'
FINGERPRINT = scoped_credential_fingerprint(TOKEN, SCOPE)
CONTENT_HASH = "a" * 64


def _state(*, fingerprint: str = FINGERPRINT, lifecycle: PairingLifecycleState = PairingLifecycleState.PAIRED) -> NodePairingState:
    return NodePairingState(
        device_id=DEVICE_ID,
        pairing_id=PAIRING_ID,
        lifecycle=lifecycle,
        credential_fingerprint=fingerprint,
        credential_scope_digest=hashlib.sha256(SCOPE.encode()).hexdigest(),
        paired_at=NOW - timedelta(minutes=1),
    )


def _request(
    *,
    sequence: int = 1,
    request_id: str = "request-1",
    captured_at: datetime = NOW,
    content_hash: str = CONTENT_HASH,
    media_type: str = "image/png",
    content_size: int = 100,
    policy_version: str = DEFAULT_POLICY_VERSION,
    capability_scope: str = "presence.read",
    data_purpose: str = "screen_capture",
    credential_fingerprint: str = FINGERPRINT,
    source_path: str | None = "/Users/operator/private.png",
    action_authority: bool = False,
) -> NodePairingRequest:
    return NodePairingRequest(
        device_id=DEVICE_ID,
        pairing_id=PAIRING_ID,
        request_id=request_id,
        sequence=sequence,
        captured_at=captured_at,
        content_hash=content_hash,
        media_type=media_type,
        content_size=content_size,
        policy_version=policy_version,
        capability_scope=capability_scope,
        data_purpose=data_purpose,
        credential_fingerprint=credential_fingerprint,
        source_path=source_path,
        action_authority=action_authority,
    )


def test_request_and_results_are_immutable_and_digest_is_stable():
    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.sequence = 2  # type: ignore[misc]

    digest = canonical_request_digest(request)
    equivalent = _request(captured_at=NOW.astimezone(timezone(timedelta(hours=2))))
    assert digest == canonical_request_digest(equivalent)
    assert digest != canonical_request_digest(_request(content_hash="b" * 64))


def test_scope_bound_fingerprint_never_uses_same_digest_for_other_scope():
    first = scoped_credential_fingerprint(TOKEN, "scope-a")
    second = scoped_credential_fingerprint(TOKEN, "scope-b")
    assert first != second
    assert len(first) == 64
    with pytest.raises(ValueError):
        scoped_credential_fingerprint("", "scope-a")


def test_valid_ingress_advances_replay_ledger_and_duplicate_is_safe():
    state = _state()
    request = _request()
    result = validate_pairing_request(request, state, now=NOW)
    assert result.status is PairingIngressStatus.ACCEPTED
    outcome = ingest_pairing_request(state, request, now=NOW)
    assert outcome.result == result
    assert outcome.state.last_sequence == 1
    assert len(outcome.state.replay_entries) == 1

    duplicate = validate_pairing_request(request, outcome.state, now=NOW)
    assert duplicate.status is PairingIngressStatus.DUPLICATE
    assert duplicate.retryable is False
    assert ingest_pairing_request(outcome.state, request, now=NOW).state == outcome.state


@pytest.mark.asyncio
async def test_authenticated_edge_ingress_returns_persisted_owner_principal(monkeypatch):
    """A valid paired request must build an owner-bound envelope."""

    extension_id = "seraph.test-edge"
    reference = "connectors/nodes/device.yaml"
    name = "test-edge"
    credential = "edge-test-credential"
    scope = paired_edge.canonical_edge_scope(
        device_id=DEVICE_ID,
        pairing_id=PAIRING_ID,
        capability_scope="media.ingest",
        data_purpose="screen_capture",
    )
    state = NodePairingState(
        device_id=DEVICE_ID,
        pairing_id=PAIRING_ID,
        lifecycle=PairingLifecycleState.PAIRED,
        credential_fingerprint=scoped_credential_fingerprint(credential, scope),
        credential_scope_digest=hashlib.sha256(scope.encode()).hexdigest(),
        paired_at=NOW,
    )
    credential_key = paired_edge._credential_key(extension_id, reference, PAIRING_ID, credential)
    entry = paired_edge.pairing_entry_from_state(
        state,
        base_entry={"name": name, "reference": reference},
        credential_ref=(
            f"{paired_edge.PAIRING_CREDENTIAL_PREFIX}"
            f"{hashlib.sha256(credential_key.encode()).hexdigest()[:24]}"
        ),
        credential_scope=scope,
        owner_principal_id="operator:test-edge",
    )
    payload = {"revision": 7, "extensions": {extension_id: {"node_pairings": {reference: entry}}}}

    async def get_credential(key: str) -> str | None:
        return credential if key == credential_key else None

    monkeypatch.setattr(paired_edge.vault_repository, "get", get_credential)
    authenticated = await paired_edge.authenticate_edge_request(
        payload,
        extension_id=extension_id,
        reference=reference,
        name=name,
        device_id=DEVICE_ID,
        pairing_id=PAIRING_ID,
        request_id="edge-request-1",
        sequence=1,
        captured_at=NOW,
        content_hash=CONTENT_HASH,
        media_type="application/json",
        content_size=0,
        capability_scope="media.ingest",
        data_purpose="screen_capture",
        policy_version=DEFAULT_POLICY_VERSION,
        presented_credential=credential,
    )

    assert authenticated.owner_principal_id == "operator:test-edge"
    assert authenticated.request.request_id == "edge-request-1"


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("content_hash", "bad", PairingIngressStatus.BLOCKED),
        ("media_type", "IMAGE/PNG", PairingIngressStatus.BLOCKED),
        ("policy_version", "other-policy", PairingIngressStatus.BLOCKED),
        ("capability_scope", "device.execute", PairingIngressStatus.BLOCKED),
        ("data_purpose", "untrusted-purpose", PairingIngressStatus.BLOCKED),
        ("action_authority", True, PairingIngressStatus.BLOCKED),
    ],
)
def test_invalid_scope_hash_media_policy_and_authority_fail_closed(field, value, status):
    request = _request(**{field: value})
    result = validate_pairing_request(request, _state(), now=NOW)
    assert result.status is status
    assert result.accepted is False
    assert result.terminal is True


def test_oversize_expired_future_and_sequence_replay_outcomes_are_distinct():
    assert validate_pairing_request(_request(content_size=NodePairingPolicy().max_content_bytes + 1), _state(), now=NOW).status is PairingIngressStatus.OVERSIZED
    assert validate_pairing_request(_request(captured_at=NOW - timedelta(seconds=301)), _state(), now=NOW).status is PairingIngressStatus.EXPIRED
    future = validate_pairing_request(_request(captured_at=NOW + timedelta(seconds=31)), _state(), now=NOW)
    assert future.status is PairingIngressStatus.RETRYABLE
    assert future.retryable is True

    accepted = ingest_pairing_request(_state(), _request(), now=NOW)
    out_of_order = validate_pairing_request(_request(sequence=1, request_id="request-2"), accepted.state, now=NOW)
    assert out_of_order.status is PairingIngressStatus.OUT_OF_ORDER


def test_policy_bounds_and_replay_window_fail_closed():
    invalid_policy = NodePairingPolicy(max_content_bytes=0)
    invalid = validate_pairing_request(_request(), _state(), invalid_policy, now=NOW)
    assert invalid.status is PairingIngressStatus.BLOCKED
    assert invalid.reason_code == "invalid_policy"

    policy = NodePairingPolicy(replay_window=1)
    first = ingest_pairing_request(_state(), _request(sequence=1), policy, now=NOW)
    second = ingest_pairing_request(first.state, _request(sequence=2, request_id="request-2"), policy, now=NOW)
    assert len(second.state.replay_entries) == 1
    old = validate_pairing_request(_request(sequence=1), second.state, policy, now=NOW)
    assert old.status is PairingIngressStatus.OUT_OF_ORDER


def test_replayed_request_id_with_changed_metadata_is_blocked():
    accepted = ingest_pairing_request(_state(), _request(), now=NOW)
    conflict = validate_pairing_request(
        _request(request_id="request-1", content_hash="b" * 64),
        accepted.state,
        now=NOW,
    )
    assert conflict.status is PairingIngressStatus.BLOCKED
    assert conflict.reason_code == "request_id_conflict"


@pytest.mark.parametrize(
    "attribute, value",
    [
        ("replay_entries", None),
        ("replay_entries", (None,)),
        ("last_sequence", -1),
        ("last_sequence", True),
    ],
)
def test_malformed_state_is_blocked_before_replay_access(attribute, value):
    malformed = _state()
    object.__setattr__(malformed, attribute, value)
    result = validate_pairing_request(_request(), malformed, now=NOW)
    assert result.status is PairingIngressStatus.BLOCKED
    assert result.reason_code == "invalid_pairing_state"


def test_revoked_expired_and_unknown_credentials_fail_closed():
    revoked = validate_pairing_request(_request(), _state(lifecycle=PairingLifecycleState.REVOKED), now=NOW)
    assert revoked.status is PairingIngressStatus.REVOKED
    expired = validate_pairing_request(_request(), _state(lifecycle=PairingLifecycleState.EXPIRED), now=NOW)
    assert expired.status is PairingIngressStatus.EXPIRED
    unknown = validate_pairing_request(_request(credential_fingerprint="b" * 64), _state(), now=NOW)
    assert unknown.status is PairingIngressStatus.BLOCKED


def test_pair_rotate_revoke_and_expire_transitions_are_deterministic():
    unpaired = NodePairingState(device_id=DEVICE_ID, pairing_id=PAIRING_ID)
    paired = apply_pairing_transition(
        unpaired,
        PairingLifecycleAction.PAIR,
        new_credential_fingerprint=FINGERPRINT,
        credential_scope=SCOPE,
        now=NOW,
    )
    assert paired.status is PairingIngressStatus.ACCEPTED
    assert paired.state.lifecycle is PairingLifecycleState.PAIRED
    assert paired.state.last_sequence == 0

    new_fingerprint = scoped_credential_fingerprint(TOKEN, SCOPE + "-rotated")
    rotated = apply_pairing_transition(
        paired.state,
        "rotate",
        current_credential_fingerprint=FINGERPRINT,
        new_credential_fingerprint=new_fingerprint,
        now=NOW + timedelta(seconds=1),
    )
    assert rotated.accepted is True
    assert rotated.state.credential_fingerprint == new_fingerprint
    stale = validate_pairing_request(_request(credential_fingerprint=FINGERPRINT), rotated.state, now=NOW)
    assert stale.status is PairingIngressStatus.REVOKED

    expired = apply_pairing_transition(rotated.state, "expire", now=NOW + timedelta(seconds=2))
    assert expired.state.lifecycle is PairingLifecycleState.EXPIRED
    assert expired.state.credential_fingerprint is None
    revoked = apply_pairing_transition(paired.state, "revoke", now=NOW + timedelta(seconds=3))
    assert revoked.state.lifecycle is PairingLifecycleState.REVOKED
    assert revoked.state.credential_fingerprint is None
    assert apply_pairing_transition(revoked.state, "pair", new_credential_fingerprint=FINGERPRINT, now=NOW).accepted is False


def test_pairing_requires_canonical_scope_and_rejects_cross_scope_reuse():
    unpaired = NodePairingState(device_id=DEVICE_ID, pairing_id=PAIRING_ID)
    missing_scope = apply_pairing_transition(
        unpaired,
        "pair",
        new_credential_fingerprint=FINGERPRINT,
        now=NOW,
    )
    assert missing_scope.status is PairingIngressStatus.BLOCKED
    assert missing_scope.reason_code == "credential_scope_required"
    assert missing_scope.state.lifecycle is PairingLifecycleState.UNPAIRED

    paired = apply_pairing_transition(
        unpaired,
        "pair",
        new_credential_fingerprint=FINGERPRINT,
        credential_scope=SCOPE,
        now=NOW,
    ).state
    cross_scope = validate_pairing_request(
        _request(capability_scope="media.ingest", data_purpose="media_analysis"),
        paired,
        now=NOW,
    )
    assert cross_scope.status is PairingIngressStatus.BLOCKED
    assert cross_scope.reason_code == "credential_scope_mismatch"


def test_receipts_redact_path_and_credentials_and_state_has_no_raw_token():
    request = _request()
    outcome = ingest_pairing_request(_state(), request, now=NOW)
    receipt = serialize_pairing_receipt(request, outcome.result, state=outcome.state)
    encoded = json.dumps(receipt, sort_keys=True)
    assert "/Users/operator" not in encoded
    assert TOKEN not in encoded
    assert FINGERPRINT not in encoded
    assert receipt["capture"]["source_path"] == "[redacted path]"
    assert receipt["capture"]["content_redacted"] is True
    assert receipt["transport"]["authentication_claimed"] is False
    assert receipt["transport"]["live_mac_transport_claimed"] is False
    transition = apply_pairing_transition(_state(), "rotate", current_credential_fingerprint=FINGERPRINT, new_credential_fingerprint="b" * 64, now=NOW)
    transition_receipt = serialize_transition_receipt(transition, state_before=_state())
    assert transition_receipt["transition"]["state"] == "paired"
    assert "retired_credential_count" in transition_receipt["lifecycle"]
