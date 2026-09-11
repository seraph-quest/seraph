"""Durable, provider-free transport for a paired observation edge.

The pairing contract in :mod:`node_pairing` intentionally has no I/O.  This
module is the narrow adapter that binds it to the extension-state JSON, the
encrypted vault, and the server-owned artifact table.  It never treats a
credential fingerprint as an authenticator: edge requests present the raw
credential over the authenticated origin and the server derives the scoped
fingerprint only after comparing it with the vault value.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.db.models import PairedEdgeArtifact
from src.extensions.node_pairing import (
    DEFAULT_ALLOWED_CAPABILITIES,
    DEFAULT_ALLOWED_DATA_PURPOSES,
    DEFAULT_ALLOWED_MEDIA_TYPES,
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    DEFAULT_MAX_CONTENT_BYTES,
    DEFAULT_POLICY_VERSION,
    DEFAULT_REPLAY_WINDOW,
    NodePairingPolicy,
    NodePairingRequest,
    NodePairingState,
    PairingIngressStatus,
    PairingLifecycleState,
    PairingReplayEntry,
    apply_pairing_transition,
    ingest_pairing_request,
    scoped_credential_fingerprint,
)
from src.extensions.state import (
    extension_state_entry,
    load_extension_state_payload,
    node_adapter_pairing_entry,
    save_extension_state_payload,
    set_node_adapter_pairing_entry,
)
from src.vault.repository import vault_repository


PAIRING_CREDENTIAL_PREFIX = "vault://seraph-node-pairing-"
DEFAULT_NODE_EXTENSION_ID = "seraph.openclaw-device-bridge"
DEFAULT_NODE_REFERENCE = "connectors/nodes/device.yaml"
EDGE_ARTIFACT_PREFIX = "edge_art_"


def _aware(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def canonical_edge_scope(
    *,
    device_id: str,
    pairing_id: str,
    capability_scope: str,
    data_purpose: str,
) -> str:
    """Encode the same canonical scope binding used by the pure contract."""

    return json.dumps(
        [device_id, pairing_id, capability_scope, data_purpose],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _credential_key(
    extension_id: str,
    reference: str,
    pairing_id: str,
    credential: str,
) -> str:
    """Return a vault key unique to one credential generation.

    Pairing mutations use an optimistic state CAS. A pairing-only key would
    let a losing concurrent mutation overwrite the winner's secret before its
    state write fails, leaving the active state unusable. Binding the key to
    the raw credential keeps generations isolated; only the generation whose
    state CAS succeeds is referenced by ``credential_ref``.
    """
    digest = hashlib.sha256(
        f"{extension_id}\x00{reference}\x00{pairing_id}\x00{credential}".encode("utf-8")
    ).hexdigest()[:40]
    return f"seraph-node-pairing-{digest}"


def _legacy_credential_key(extension_id: str, reference: str, pairing_id: str) -> str:
    """Key format used before credential generations were isolated."""
    digest = hashlib.sha256(
        f"{extension_id}\x00{reference}\x00{pairing_id}".encode("utf-8")
    ).hexdigest()[:40]
    return f"seraph-node-pairing-{digest}"


def _policy_from_entry(entry: dict[str, Any] | None) -> NodePairingPolicy:
    raw = entry.get("policy") if isinstance(entry, dict) else None
    if not isinstance(raw, dict):
        return NodePairingPolicy()

    def _positive_int(name: str, default: int) -> int:
        value = raw.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default

    def _tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = raw.get(name)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return default
        return tuple(value)

    return NodePairingPolicy(
        policy_version=str(raw.get("policy_version") or entry.get("policy_version") or DEFAULT_POLICY_VERSION),
        max_age_seconds=_positive_int("max_age_seconds", DEFAULT_MAX_AGE_SECONDS),
        max_clock_skew_seconds=_positive_int("max_clock_skew_seconds", DEFAULT_MAX_CLOCK_SKEW_SECONDS),
        max_content_bytes=_positive_int("max_content_bytes", DEFAULT_MAX_CONTENT_BYTES),
        replay_window=_positive_int("replay_window", DEFAULT_REPLAY_WINDOW),
        allowed_media_types=_tuple("allowed_media_types", DEFAULT_ALLOWED_MEDIA_TYPES),
        allowed_capabilities=_tuple("allowed_capabilities", DEFAULT_ALLOWED_CAPABILITIES),
        allowed_data_purposes=_tuple("allowed_data_purposes", DEFAULT_ALLOWED_DATA_PURPOSES),
    )


def _lifecycle(value: Any) -> PairingLifecycleState:
    try:
        return PairingLifecycleState(str(value))
    except (TypeError, ValueError):
        return PairingLifecycleState.UNPAIRED


def pairing_state_from_entry(
    entry: dict[str, Any] | None,
    *,
    device_id: str,
    pairing_id: str,
) -> NodePairingState:
    """Decode the bounded JSON pairing snapshot used by the pure contract."""

    raw = entry if isinstance(entry, dict) else {}
    replay_entries: list[PairingReplayEntry] = []
    raw_replay = raw.get("replay_entries")
    if isinstance(raw_replay, list):
        for item in raw_replay:
            if not isinstance(item, dict):
                continue
            accepted_at = parse_timestamp(item.get("accepted_at"))
            request_id = item.get("request_id")
            sequence = item.get("sequence")
            digest = item.get("request_digest")
            if (
                accepted_at is not None
                and isinstance(request_id, str)
                and isinstance(sequence, int)
                and not isinstance(sequence, bool)
                and isinstance(digest, str)
            ):
                replay_entries.append(
                    PairingReplayEntry(
                        request_id=request_id,
                        sequence=sequence,
                        request_digest=digest.removeprefix("sha256:"),
                        accepted_at=accepted_at,
                    )
                )
    last_sequence = raw.get("last_sequence", 0)
    generation = raw.get("generation", raw.get("revision", 0))
    return NodePairingState(
        device_id=device_id,
        pairing_id=pairing_id,
        lifecycle=_lifecycle(raw.get("lifecycle", raw.get("pairing_state", "unpaired"))),
        credential_fingerprint=(
            str(raw.get("credential_fingerprint"))
            if isinstance(raw.get("credential_fingerprint"), str)
            else None
        ),
        credential_scope_digest=(
            str(raw.get("credential_scope_digest"))
            if isinstance(raw.get("credential_scope_digest"), str)
            else None
        ),
        policy_version=str(raw.get("policy_version") or DEFAULT_POLICY_VERSION),
        last_sequence=last_sequence if isinstance(last_sequence, int) and not isinstance(last_sequence, bool) else 0,
        replay_entries=tuple(replay_entries),
        retired_credential_fingerprints=tuple(
            item
            for item in raw.get("retired_credential_fingerprints", [])
            if isinstance(item, str)
        ) if isinstance(raw.get("retired_credential_fingerprints"), list) else (),
        generation=generation if isinstance(generation, int) and not isinstance(generation, bool) else 0,
        paired_at=parse_timestamp(raw.get("paired_at")),
        expires_at=parse_timestamp(raw.get("expires_at")),
        revoked_at=parse_timestamp(raw.get("revoked_at")),
    )


def pairing_entry_from_state(
    state: NodePairingState,
    *,
    base_entry: dict[str, Any] | None = None,
    credential_ref: str | None = None,
    credential_scope: str | None = None,
    owner_principal_id: str | None = None,
    label: str | None = None,
    policy: NodePairingPolicy | None = None,
) -> dict[str, Any]:
    """Encode state without retaining raw credentials or source paths."""

    entry = dict(base_entry) if isinstance(base_entry, dict) else {}
    entry.update(
        {
            "device_id": state.device_id,
            "pairing_id": state.pairing_id,
            "pairing_state": state.lifecycle.value,
            "lifecycle": state.lifecycle.value,
            "trusted": state.lifecycle is PairingLifecycleState.PAIRED,
            "trust_state": "trusted" if state.lifecycle is PairingLifecycleState.PAIRED else "untrusted" if state.lifecycle in {PairingLifecycleState.REVOKED, PairingLifecycleState.EXPIRED} else "unpaired",
            "revoked": state.lifecycle is PairingLifecycleState.REVOKED,
            "credential_fingerprint": state.credential_fingerprint,
            "credential_scope_digest": state.credential_scope_digest,
            "policy_version": state.policy_version,
            "last_sequence": state.last_sequence,
            "generation": state.generation,
            "revision": state.generation,
            "paired_at": state.paired_at.isoformat() if state.paired_at else None,
            "expires_at": state.expires_at.isoformat() if state.expires_at else None,
            "revoked_at": state.revoked_at.isoformat() if state.revoked_at else None,
            "retired_credential_fingerprints": list(state.retired_credential_fingerprints),
            "replay_entries": [
                {
                    "request_id": item.request_id,
                    "sequence": item.sequence,
                    "request_digest": item.request_digest,
                    "accepted_at": item.accepted_at.isoformat(),
                }
                for item in state.replay_entries
            ],
            "action_authority": False,
        }
    )
    if credential_ref is not None:
        entry["credential_ref"] = credential_ref
        entry["credential_configured"] = True
    if credential_scope is not None:
        entry["credential_scope"] = credential_scope
    if owner_principal_id is not None:
        entry["owner_principal_id"] = owner_principal_id
    if label is not None:
        entry["label"] = label
    effective_policy = policy or _policy_from_entry(entry)
    entry["policy"] = {
        "policy_version": effective_policy.policy_version,
        "max_age_seconds": effective_policy.max_age_seconds,
        "max_clock_skew_seconds": effective_policy.max_clock_skew_seconds,
        "max_content_bytes": effective_policy.max_content_bytes,
        "replay_window": effective_policy.replay_window,
        "allowed_media_types": list(effective_policy.allowed_media_types),
        "allowed_capabilities": list(effective_policy.allowed_capabilities),
        "allowed_data_purposes": list(effective_policy.allowed_data_purposes),
    }
    return entry


def current_pairing(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
) -> tuple[dict[str, Any], NodePairingState]:
    state_entry = extension_state_entry(payload, extension_id, create=False) or {}
    raw = node_adapter_pairing_entry(state_entry, reference=reference, name=name, create=False)
    entry = raw if isinstance(raw, dict) else {}
    device_id = str(entry.get("device_id") or f"device-{hashlib.sha256(f'{extension_id}:{reference}'.encode()).hexdigest()[:12]}")
    pairing_id = str(entry.get("pairing_id") or f"pairing-{hashlib.sha256(f'{extension_id}:{reference}:{device_id}'.encode()).hexdigest()[:12]}")
    return entry, pairing_state_from_entry(entry, device_id=device_id, pairing_id=pairing_id)


async def verify_pairing_credential(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
    presented_credential: str,
) -> tuple[dict[str, Any], NodePairingState]:
    """Compare a raw edge credential with the encrypted vault value.

    This helper is shared by reconnect and ingest so neither path can use a
    persisted fingerprint as authentication.
    """

    entry, state = current_pairing(
        payload,
        extension_id=extension_id,
        reference=reference,
        name=name,
    )
    credential_ref = entry.get("credential_ref")
    if not isinstance(credential_ref, str) or not credential_ref.startswith(PAIRING_CREDENTIAL_PREFIX):
        raise ValueError("credential_not_configured")
    if not isinstance(presented_credential, str) or not presented_credential:
        raise ValueError("authentication_required")
    key = _credential_key(extension_id, reference, state.pairing_id, presented_credential)
    expected_ref = f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    if not hmac.compare_digest(credential_ref, expected_ref):
        legacy_key = _legacy_credential_key(extension_id, reference, state.pairing_id)
        legacy_ref = f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(legacy_key.encode()).hexdigest()[:24]}"
        if not hmac.compare_digest(credential_ref, legacy_ref):
            raise ValueError("credential_ref_invalid")
        key = legacy_key
    stored = await vault_repository.get(key)
    if stored is None:
        raise ValueError("credential_unavailable")
    if not hmac.compare_digest(stored, presented_credential):
        raise ValueError("authentication_failed")
    owner_principal_id = str(entry.get("owner_principal_id") or "").strip()
    if not owner_principal_id:
        raise ValueError("pairing_owner_missing")
    return entry, state


def _token() -> str:
    return secrets.token_urlsafe(32)


async def create_pairing(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
    device_id: str,
    pairing_id: str,
    owner_principal_id: str,
    label: str | None = None,
    capability_scope: str = "media.ingest",
    data_purpose: str = "screen_capture",
    policy: NodePairingPolicy | None = None,
    expected_revision: int | None = None,
) -> tuple[dict[str, Any], str, int]:
    """Pair an edge and return its one-time raw credential to the caller."""

    _, current = current_pairing(payload, extension_id=extension_id, reference=reference, name=name)
    effective_policy = policy or NodePairingPolicy()
    credential_scope = canonical_edge_scope(
        device_id=device_id,
        pairing_id=pairing_id,
        capability_scope=capability_scope,
        data_purpose=data_purpose,
    )
    raw_credential = _token()
    fingerprint = scoped_credential_fingerprint(raw_credential, credential_scope)
    transition = apply_pairing_transition(
        current if current.device_id == device_id and current.pairing_id == pairing_id else NodePairingState(device_id=device_id, pairing_id=pairing_id),
        "pair",
        new_credential_fingerprint=fingerprint,
        credential_scope=credential_scope,
        policy=effective_policy,
    )
    if not transition.accepted:
        raise ValueError(transition.reason_code)
    key = _credential_key(extension_id, reference, pairing_id, raw_credential)
    await vault_repository.store(key, raw_credential, description="paired edge credential")
    credential_ref = f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    entry = pairing_entry_from_state(
        transition.state,
        base_entry={"name": name, "reference": reference},
        credential_ref=credential_ref,
        credential_scope=credential_scope,
        owner_principal_id=owner_principal_id,
        label=label,
        policy=effective_policy,
    )
    set_node_adapter_pairing_entry(
        payload,
        extension_id=extension_id,
        reference=reference,
        name=name,
        pairing=entry,
    )
    persisted_revision = save_extension_state_payload(payload, expected_revision=expected_revision)
    return entry, raw_credential, persisted_revision


async def rotate_pairing(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
    current_credential: str,
    expected_revision: int | None = None,
) -> tuple[dict[str, Any], str, int]:
    entry, current = current_pairing(payload, extension_id=extension_id, reference=reference, name=name)
    scope = entry.get("credential_scope")
    if not isinstance(scope, str) or not scope:
        raise ValueError("credential_scope_missing")
    active_fingerprint = scoped_credential_fingerprint(current_credential, scope)
    stored_ref = entry.get("credential_ref")
    if not isinstance(stored_ref, str) or not stored_ref.startswith(PAIRING_CREDENTIAL_PREFIX):
        raise ValueError("credential_ref_missing")
    # The vault key is deterministic from the presented generation but is
    # deliberately not exposed in state.
    key = _credential_key(extension_id, reference, current.pairing_id, current_credential)
    expected_ref = f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    if not hmac.compare_digest(stored_ref, expected_ref):
        legacy_key = _legacy_credential_key(extension_id, reference, current.pairing_id)
        legacy_ref = f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(legacy_key.encode()).hexdigest()[:24]}"
        if not hmac.compare_digest(stored_ref, legacy_ref):
            raise ValueError("credential_ref_invalid")
        key = legacy_key
    stored = await vault_repository.get(key)
    if stored is None or not hmac.compare_digest(stored, current_credential):
        raise ValueError("current_credential_invalid")
    if current.credential_fingerprint and not hmac.compare_digest(active_fingerprint, current.credential_fingerprint):
        raise ValueError("current_credential_invalid")
    raw_credential = _token()
    new_fingerprint = scoped_credential_fingerprint(raw_credential, scope)
    transition = apply_pairing_transition(
        current,
        "rotate",
        current_credential_fingerprint=active_fingerprint,
        new_credential_fingerprint=new_fingerprint,
    )
    if not transition.accepted:
        raise ValueError(transition.reason_code)
    new_key = _credential_key(extension_id, reference, current.pairing_id, raw_credential)
    await vault_repository.store(new_key, raw_credential, description="paired edge credential")
    entry = pairing_entry_from_state(
        transition.state,
        base_entry=entry,
        credential_ref=(
            f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(new_key.encode()).hexdigest()[:24]}"
        ),
        credential_scope=scope,
        owner_principal_id=str(entry.get("owner_principal_id") or ""),
        policy=_policy_from_entry(entry),
    )
    set_node_adapter_pairing_entry(
        payload,
        extension_id=extension_id,
        reference=reference,
        name=name,
        pairing=entry,
    )
    persisted_revision = save_extension_state_payload(payload, expected_revision=expected_revision)
    return entry, raw_credential, persisted_revision


@dataclass(frozen=True)
class EdgeAuthenticatedRequest:
    request: NodePairingRequest
    state: NodePairingState
    entry: dict[str, Any]
    owner_principal_id: str
    policy: NodePairingPolicy
    payload_revision: int


async def authenticate_edge_request(
    payload: dict[str, Any],
    *,
    extension_id: str,
    reference: str,
    name: str,
    device_id: str,
    pairing_id: str,
    request_id: str,
    sequence: int,
    captured_at: datetime,
    content_hash: str,
    media_type: str,
    content_size: int,
    capability_scope: str,
    data_purpose: str,
    policy_version: str,
    presented_credential: str,
    source_path: str | None = None,
    action_authority: bool = False,
) -> EdgeAuthenticatedRequest:
    """Authenticate a request and build the pure contract envelope."""

    entry, state = current_pairing(payload, extension_id=extension_id, reference=reference, name=name)
    credential_scope = entry.get("credential_scope")
    credential_ref = entry.get("credential_ref")
    if not isinstance(credential_scope, str) or not isinstance(credential_ref, str):
        raise ValueError("credential_not_configured")
    if not isinstance(presented_credential, str) or not presented_credential:
        raise ValueError("authentication_required")
    key = _credential_key(extension_id, reference, state.pairing_id, presented_credential)
    expected_ref = f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    if not hmac.compare_digest(credential_ref, expected_ref):
        legacy_key = _legacy_credential_key(extension_id, reference, state.pairing_id)
        legacy_ref = f"{PAIRING_CREDENTIAL_PREFIX}{hashlib.sha256(legacy_key.encode()).hexdigest()[:24]}"
        if not hmac.compare_digest(credential_ref, legacy_ref):
            raise ValueError("credential_ref_invalid")
        key = legacy_key
    stored = await vault_repository.get(key)
    if stored is None or not isinstance(presented_credential, str) or not presented_credential:
        raise ValueError("authentication_required")
    if not hmac.compare_digest(stored, presented_credential):
        raise ValueError("authentication_failed")
    fingerprint = scoped_credential_fingerprint(presented_credential, credential_scope)
    request = NodePairingRequest(
        device_id=device_id,
        pairing_id=pairing_id,
        request_id=request_id,
        sequence=sequence,
        captured_at=captured_at,
        content_hash=content_hash,
        media_type=media_type,
        content_size=content_size,
        policy_version=policy_version,
        capability_scope=capability_scope,
        data_purpose=data_purpose,
        credential_fingerprint=fingerprint,
        source_path=source_path,
        action_authority=action_authority,
    )
    return EdgeAuthenticatedRequest(
        request=request,
        state=state,
        entry=entry,
        owner_principal_id=owner_principal_id,
        policy=_policy_from_entry(entry),
        payload_revision=int(payload.get("revision") or 0),
    )


def decode_content(value: Any) -> bytes:
    if value in (None, ""):
        return b""
    if not isinstance(value, str):
        raise ValueError("content_base64_required")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("content_base64_invalid") from exc


def content_hash(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def server_artifact_id() -> str:
    return f"{EDGE_ARTIFACT_PREFIX}{uuid4().hex}"


def artifact_metadata(artifact: PairedEdgeArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "readback_id": artifact.artifact_id,
        "request_id": artifact.request_id,
        "device_id": artifact.device_id,
        "pairing_id": artifact.pairing_id,
        "sequence": artifact.sequence,
        "captured_at": _aware(artifact.captured_at).isoformat() if _aware(artifact.captured_at) else None,
        "content_hash": artifact.content_hash,
        "media_type": artifact.media_type,
        "size_bytes": artifact.content_size,
        "owner_principal_id": artifact.owner_principal_id,
        "source_path": None,
        "server_owned": True,
        "created_at": _aware(artifact.created_at).isoformat() if _aware(artifact.created_at) else None,
    }


__all__ = [
    "DEFAULT_NODE_EXTENSION_ID",
    "DEFAULT_NODE_REFERENCE",
    "EDGE_ARTIFACT_PREFIX",
    "EdgeAuthenticatedRequest",
    "PAIRING_CREDENTIAL_PREFIX",
    "artifact_metadata",
    "authenticate_edge_request",
    "canonical_edge_scope",
    "content_hash",
    "create_pairing",
    "current_pairing",
    "decode_content",
    "pairing_entry_from_state",
    "pairing_state_from_entry",
    "parse_timestamp",
    "rotate_pairing",
    "server_artifact_id",
    "verify_pairing_credential",
]
