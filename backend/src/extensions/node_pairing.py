"""Provider-free pairing and ingress contracts for an external Seraph node.

This module is deliberately a pure contract layer.  It does not open a socket,
authenticate an HTTP request, read an artifact, or persist pairing state.  A
transport adapter can use the immutable types and pure helpers here before it
hands an accepted capture to the governed runtime.

The credential value accepted by this module is a scoped fingerprint.  Callers
must derive it with :func:`scoped_credential_fingerprint` and must never pass a
raw token to a request, transition, receipt, or persistence helper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final


NODE_PAIRING_SCHEMA_VERSION: Final = "seraph.node_pairing.v1"
NODE_PAIRING_RECEIPT_SCHEMA_VERSION: Final = "seraph.node_pairing.receipt.v1"
DEFAULT_POLICY_VERSION: Final = "node-pairing-policy.v1"
DEFAULT_MAX_AGE_SECONDS: Final = 300
DEFAULT_MAX_CLOCK_SKEW_SECONDS: Final = 30
DEFAULT_MAX_CONTENT_BYTES: Final = 25 * 1024 * 1024
DEFAULT_REPLAY_WINDOW: Final = 64

DEFAULT_ALLOWED_MEDIA_TYPES: Final[tuple[str, ...]] = (
    "application/json",
    "audio/mpeg",
    "audio/wav",
    "image/jpeg",
    "image/png",
    "image/webp",
)
DEFAULT_ALLOWED_CAPABILITIES: Final[tuple[str, ...]] = (
    "media.ingest",
    "presence.read",
)
DEFAULT_ALLOWED_DATA_PURPOSES: Final[tuple[str, ...]] = (
    "media_analysis",
    "presence",
    "screen_capture",
)

_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_POLICY_VERSION_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_MEDIA_TYPE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*(?:;[a-z0-9][a-z0-9!#$&^_.+-]*=[a-z0-9][a-z0-9!#$&^_.+-]*)*$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_MAX_TOKEN_BYTES: Final = 4096
_MAX_SCOPE_BYTES: Final = 512
_MAX_RETIRED_FINGERPRINTS: Final = 32


class PairingIngressStatus(str, Enum):
    """Stable result states for a node ingress request."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    EXPIRED = "expired"
    REVOKED = "revoked"
    OVERSIZED = "oversized"
    BLOCKED = "blocked"
    RETRYABLE = "retryable"


class PairingLifecycleState(str, Enum):
    """Lifecycle state of a pairing identity in the core."""

    UNPAIRED = "unpaired"
    PAIRED = "paired"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PairingLifecycleAction(str, Enum):
    """Operator-owned state changes supported by this contract."""

    PAIR = "pair"
    ROTATE = "rotate"
    REVOKE = "revoke"
    EXPIRE = "expire"


@dataclass(frozen=True, slots=True)
class NodePairingPolicy:
    """Finite policy used by the pure ingress validator.

    The allow-lists are tuples so a policy can be passed across a process
    boundary without carrying mutable collections.  Invalid values are
    rejected by validation with ``blocked``; constructing a typed policy does
    not silently repair an operator's policy.
    """

    policy_version: str = DEFAULT_POLICY_VERSION
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES
    replay_window: int = DEFAULT_REPLAY_WINDOW
    allowed_media_types: tuple[str, ...] = DEFAULT_ALLOWED_MEDIA_TYPES
    allowed_capabilities: tuple[str, ...] = DEFAULT_ALLOWED_CAPABILITIES
    allowed_data_purposes: tuple[str, ...] = DEFAULT_ALLOWED_DATA_PURPOSES
    allow_action_authority: bool = False

    @property
    def max_size_bytes(self) -> int:
        """Compatibility name for consumers that call the bound a size."""

        return self.max_content_bytes


@dataclass(frozen=True, slots=True)
class NodePairingRequest:
    """Metadata envelope for one consented edge observation.

    ``source_path`` is optional transport metadata and is intentionally not a
    digest input.  Raw payload bytes and raw credentials are not fields of this
    type; the transport must verify bytes against ``content_hash`` before it
    invokes the governed artifact path.
    """

    device_id: str
    pairing_id: str
    request_id: str
    sequence: int
    captured_at: datetime
    content_hash: str
    media_type: str
    content_size: int
    policy_version: str
    capability_scope: str
    data_purpose: str
    credential_fingerprint: str
    source_path: str | None = None
    action_authority: bool = False

    @property
    def capability(self) -> str:
        """Short alias used by capability adapters."""

        return self.capability_scope

    @property
    def data_purpose_scope(self) -> str:
        return self.data_purpose

    @property
    def monotonic_sequence(self) -> int:
        return self.sequence

    @property
    def capture_timestamp(self) -> datetime:
        return self.captured_at

    @property
    def size_bytes(self) -> int:
        return self.content_size

    @property
    def scope_key(self) -> str:
        """Canonical scope binding for credential fingerprint derivation."""

        return _scope_key(
            device_id=self.device_id,
            pairing_id=self.pairing_id,
            capability_scope=self.capability_scope,
            data_purpose=self.data_purpose,
        )


@dataclass(frozen=True, slots=True)
class PairingReplayEntry:
    """Bound metadata needed to reject a replay within the sequence window."""

    request_id: str
    sequence: int
    request_digest: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class NodePairingState:
    """Immutable state snapshot consumed and returned by pure helpers.

    Only derived credential fingerprints are retained.  ``retired`` contains
    fingerprints from rotation or revocation so stale credentials can receive
    the explicit ``revoked`` outcome without retaining their raw token.
    """

    device_id: str
    pairing_id: str
    lifecycle: PairingLifecycleState = PairingLifecycleState.UNPAIRED
    credential_fingerprint: str | None = None
    credential_scope_digest: str | None = None
    policy_version: str = DEFAULT_POLICY_VERSION
    last_sequence: int = 0
    replay_entries: tuple[PairingReplayEntry, ...] = ()
    retired_credential_fingerprints: tuple[str, ...] = ()
    generation: int = 0
    paired_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def state(self) -> PairingLifecycleState:
        return self.lifecycle

    @property
    def pairing_state(self) -> str:
        return self.lifecycle.value

    @property
    def active_credential_fingerprint(self) -> str | None:
        return self.credential_fingerprint


@dataclass(frozen=True, slots=True)
class PairingValidationResult:
    """Typed result for one ingress validation decision."""

    status: PairingIngressStatus
    reason_code: str
    accepted: bool
    retryable: bool
    request_digest: str | None = None
    last_sequence: int | None = None

    @property
    def terminal(self) -> bool:
        return not self.retryable


@dataclass(frozen=True, slots=True)
class PairingIngressOutcome:
    """Validation result plus the next immutable state snapshot."""

    result: PairingValidationResult
    state: NodePairingState


@dataclass(frozen=True, slots=True)
class PairingTransitionResult:
    """Typed result for pair, rotate, revoke, and expire transitions."""

    status: PairingIngressStatus
    reason_code: str
    accepted: bool
    state: NodePairingState

    @property
    def retryable(self) -> bool:
        return self.status is PairingIngressStatus.RETRYABLE

    @property
    def terminal(self) -> bool:
        return not self.retryable


# Friendly aliases keep adapter code readable while the explicit names remain
# useful in receipts and type checkers.
PairingPolicy = NodePairingPolicy
PairingRequest = NodePairingRequest
PairingState = NodePairingState


def _utc_datetime(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _now_or_utc(value: datetime | None) -> datetime | None:
    return _utc_datetime(value) if value is not None else datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc_datetime(value)
    if normalized is None:
        return None
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def _is_policy_version(value: Any) -> bool:
    return isinstance(value, str) and bool(_POLICY_VERSION_RE.fullmatch(value))


def _normalized_content_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.lower().startswith("sha256:"):
        candidate = candidate[7:]
    if len(candidate) != 64:
        return None
    candidate = candidate.lower()
    if not _SHA256_RE.fullmatch(candidate):
        return None
    return f"sha256:{candidate}"


def _is_media_type(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 128 and bool(_MEDIA_TYPE_RE.fullmatch(value))


def _is_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT_RE.fullmatch(value))


def _valid_policy(policy: NodePairingPolicy) -> bool:
    if not isinstance(policy, NodePairingPolicy):
        return False
    if not all(
        isinstance(value, tuple)
        for value in (
            policy.allowed_media_types,
            policy.allowed_capabilities,
            policy.allowed_data_purposes,
        )
    ):
        return False
    if not _is_policy_version(policy.policy_version):
        return False
    integer_bounds = (
        policy.max_age_seconds,
        policy.max_clock_skew_seconds,
        policy.max_content_bytes,
        policy.replay_window,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integer_bounds):
        return False
    if policy.max_age_seconds > 7 * 24 * 60 * 60:
        return False
    if policy.max_clock_skew_seconds > 24 * 60 * 60:
        return False
    if policy.max_content_bytes > 1024 * 1024 * 1024:
        return False
    if policy.replay_window > 4096:
        return False
    if not isinstance(policy.allow_action_authority, bool):
        return False
    for media_type in policy.allowed_media_types:
        if not _is_media_type(media_type) or media_type != media_type.lower():
            return False
    for scope in (*policy.allowed_capabilities, *policy.allowed_data_purposes):
        if not isinstance(scope, str) or not scope or len(scope) > 128 or any(char.isspace() for char in scope):
            return False
    return (
        not policy.allow_action_authority
        and len(set(policy.allowed_media_types)) == len(policy.allowed_media_types)
        and len(set(policy.allowed_capabilities)) == len(policy.allowed_capabilities)
        and len(set(policy.allowed_data_purposes)) == len(policy.allowed_data_purposes)
    )


def _scope_key(*, device_id: str, pairing_id: str, capability_scope: str, data_purpose: str) -> str:
    payload = [device_id, pairing_id, capability_scope, data_purpose]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _scope_digest(scope_key: str) -> str:
    return hashlib.sha256(scope_key.encode("utf-8")).hexdigest()


def scoped_credential_fingerprint(raw_token: str, scope: str) -> str:
    """Derive a non-reversible, scope-bound credential fingerprint.

    This helper returns only a SHA-256 digest and does not log, store, or place
    the raw token in an object.  The fingerprint is an identity handle, not a
    transport authenticator; HTTPS/origin/authentication belongs to the future
    edge transport adapter.
    """

    if not isinstance(raw_token, str) or not raw_token or len(raw_token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise ValueError("credential token must be a non-empty bounded string")
    if not isinstance(scope, str) or not scope or len(scope.encode("utf-8")) > _MAX_SCOPE_BYTES:
        raise ValueError("credential scope must be a non-empty bounded string")
    token_bytes = raw_token.encode("utf-8")
    scope_bytes = scope.encode("utf-8")
    return hashlib.sha256(
        b"seraph-node-credential-v1\x00" + len(scope_bytes).to_bytes(4, "big") + scope_bytes
        + len(token_bytes).to_bytes(4, "big") + token_bytes
    ).hexdigest()


def canonical_request_digest(request: NodePairingRequest) -> str:
    """Return the stable digest of request metadata, excluding raw path data."""

    if not isinstance(request, NodePairingRequest):
        raise TypeError("request must be a NodePairingRequest")
    captured_at = _utc_datetime(request.captured_at)
    if captured_at is None:
        raise ValueError("captured_at must be timezone-aware")
    canonical = {
        "schema": NODE_PAIRING_SCHEMA_VERSION,
        "device_id": request.device_id,
        "pairing_id": request.pairing_id,
        "request_id": request.request_id,
        "sequence": request.sequence,
        "captured_at": _iso(captured_at),
        "content_hash": _normalized_content_hash(request.content_hash),
        "media_type": request.media_type,
        "content_size": request.content_size,
        "policy_version": request.policy_version,
        "capability_scope": request.capability_scope,
        "data_purpose": request.data_purpose,
        "credential_fingerprint": request.credential_fingerprint,
        "action_authority": request.action_authority,
    }
    serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _result(
    status: PairingIngressStatus,
    reason_code: str,
    *,
    request_digest: str | None = None,
    last_sequence: int | None = None,
) -> PairingValidationResult:
    return PairingValidationResult(
        status=status,
        reason_code=reason_code,
        accepted=status is PairingIngressStatus.ACCEPTED,
        retryable=status is PairingIngressStatus.RETRYABLE,
        request_digest=request_digest,
        last_sequence=last_sequence,
    )


def _structural_request_error(request: NodePairingRequest, policy: NodePairingPolicy) -> str | None:
    if not _valid_policy(policy):
        return "invalid_policy"
    if not isinstance(request, NodePairingRequest):
        return "invalid_request_type"
    if not _is_identifier(request.device_id):
        return "invalid_device_id"
    if not _is_identifier(request.pairing_id):
        return "invalid_pairing_id"
    if not _is_identifier(request.request_id):
        return "invalid_request_id"
    if isinstance(request.sequence, bool) or not isinstance(request.sequence, int) or request.sequence < 1:
        return "invalid_sequence"
    if _utc_datetime(request.captured_at) is None:
        return "invalid_capture_timestamp"
    if _normalized_content_hash(request.content_hash) is None:
        return "invalid_content_hash"
    if not _is_media_type(request.media_type) or request.media_type != request.media_type.lower():
        return "invalid_media_type"
    if isinstance(request.content_size, bool) or not isinstance(request.content_size, int) or request.content_size < 0:
        return "invalid_content_size"
    if not _is_policy_version(request.policy_version):
        return "invalid_policy_version"
    if not isinstance(request.capability_scope, str) or not request.capability_scope.strip() or any(
        char.isspace() for char in request.capability_scope
    ):
        return "invalid_capability_scope"
    if not isinstance(request.data_purpose, str) or not request.data_purpose.strip() or any(
        char.isspace() for char in request.data_purpose
    ):
        return "invalid_data_purpose"
    if not _is_fingerprint(request.credential_fingerprint):
        return "invalid_credential_fingerprint"
    if not isinstance(request.action_authority, bool):
        return "invalid_action_authority"
    if request.source_path is not None and (
        not isinstance(request.source_path, str) or len(request.source_path) > 4096
    ):
        return "invalid_source_path"
    return None


def _record_matches(entry: PairingReplayEntry, request: NodePairingRequest, digest: str) -> bool:
    return (
        entry.request_id == request.request_id
        and entry.sequence == request.sequence
        and hmac.compare_digest(entry.request_digest, digest)
    )


def _valid_state_shape(state: NodePairingState, policy: NodePairingPolicy) -> bool:
    """Check the immutable state boundary before reading replay data.

    State normally comes from a typed constructor, but a persisted snapshot or
    an adapter can still contain malformed runtime values.  Validation must
    fail closed before iterating ``replay_entries`` or comparing state fields;
    otherwise a corrupt snapshot can become a server error or a replay bypass.
    """

    if not isinstance(state, NodePairingState):
        return False
    if not _is_identifier(state.device_id) or not _is_identifier(state.pairing_id):
        return False
    if not isinstance(state.lifecycle, PairingLifecycleState):
        return False
    if state.credential_fingerprint is not None and not _is_fingerprint(state.credential_fingerprint):
        return False
    if state.credential_scope_digest is not None and not _is_fingerprint(state.credential_scope_digest):
        return False
    if not _is_policy_version(state.policy_version):
        return False
    if isinstance(state.last_sequence, bool) or not isinstance(state.last_sequence, int) or state.last_sequence < 0:
        return False
    if not isinstance(state.replay_entries, tuple) or len(state.replay_entries) > policy.replay_window:
        return False
    seen_request_ids: set[str] = set()
    seen_digests: set[str] = set()
    for entry in state.replay_entries:
        if not isinstance(entry, PairingReplayEntry):
            return False
        if not _is_identifier(entry.request_id) or entry.request_id in seen_request_ids:
            return False
        if isinstance(entry.sequence, bool) or not isinstance(entry.sequence, int) or not 1 <= entry.sequence <= state.last_sequence:
            return False
        if not _is_fingerprint(entry.request_digest) or entry.request_digest in seen_digests:
            return False
        if _utc_datetime(entry.accepted_at) is None:
            return False
        seen_request_ids.add(entry.request_id)
        seen_digests.add(entry.request_digest)
    if not isinstance(state.retired_credential_fingerprints, tuple):
        return False
    if len(state.retired_credential_fingerprints) > _MAX_RETIRED_FINGERPRINTS:
        return False
    retired = state.retired_credential_fingerprints
    if any(not _is_fingerprint(value) for value in retired) or len(set(retired)) != len(retired):
        return False
    if isinstance(state.generation, bool) or not isinstance(state.generation, int) or state.generation < 0:
        return False
    for timestamp in (state.paired_at, state.expires_at, state.revoked_at):
        if timestamp is not None and _utc_datetime(timestamp) is None:
            return False
    return True


def validate_pairing_request(
    request: NodePairingRequest,
    state: NodePairingState,
    policy: NodePairingPolicy | None = None,
    *,
    now: datetime | None = None,
) -> PairingValidationResult:
    """Validate one ingress envelope against an immutable state snapshot.

    Validation order is stable: structure and policy, scope and authority,
    freshness, lifecycle and credential, then replay/sequence.  No side effect
    occurs.  A caller that receives ``accepted`` must pass the same request to
    :func:`ingest_pairing_request` or :func:`record_pairing_acceptance` to obtain
    the next state snapshot.
    """

    effective_policy = policy if policy is not None else NodePairingPolicy()
    structural_error = _structural_request_error(request, effective_policy)
    if structural_error:
        status = PairingIngressStatus.OVERSIZED if structural_error == "invalid_content_size" and isinstance(
            request, NodePairingRequest
        ) and isinstance(request.content_size, int) and not isinstance(request.content_size, bool) and request.content_size > effective_policy.max_content_bytes else PairingIngressStatus.BLOCKED
        return _result(status, structural_error)
    if not isinstance(state, NodePairingState) or not _valid_state_shape(state, effective_policy):
        return _result(PairingIngressStatus.BLOCKED, "invalid_pairing_state")
    digest = canonical_request_digest(request)
    if request.policy_version != effective_policy.policy_version or request.policy_version != state.policy_version:
        return _result(PairingIngressStatus.BLOCKED, "policy_version_mismatch", request_digest=digest, last_sequence=state.last_sequence)
    if request.capability_scope not in effective_policy.allowed_capabilities:
        return _result(PairingIngressStatus.BLOCKED, "capability_scope_not_allowed", request_digest=digest, last_sequence=state.last_sequence)
    if request.data_purpose not in effective_policy.allowed_data_purposes:
        return _result(PairingIngressStatus.BLOCKED, "data_purpose_not_allowed", request_digest=digest, last_sequence=state.last_sequence)
    if request.media_type not in effective_policy.allowed_media_types:
        return _result(PairingIngressStatus.BLOCKED, "media_type_not_allowed", request_digest=digest, last_sequence=state.last_sequence)
    if request.action_authority or effective_policy.allow_action_authority:
        return _result(PairingIngressStatus.BLOCKED, "action_authority_forbidden", request_digest=digest, last_sequence=state.last_sequence)
    if request.content_size > effective_policy.max_content_bytes:
        return _result(PairingIngressStatus.OVERSIZED, "content_size_exceeds_policy", request_digest=digest, last_sequence=state.last_sequence)

    current = _now_or_utc(now)
    if current is None:
        return _result(PairingIngressStatus.BLOCKED, "invalid_validation_clock", request_digest=digest, last_sequence=state.last_sequence)
    captured = _utc_datetime(request.captured_at)
    assert captured is not None
    age_seconds = (current - captured).total_seconds()
    if age_seconds > effective_policy.max_age_seconds:
        return _result(PairingIngressStatus.EXPIRED, "capture_too_old", request_digest=digest, last_sequence=state.last_sequence)
    if age_seconds < -effective_policy.max_clock_skew_seconds:
        return _result(PairingIngressStatus.RETRYABLE, "capture_clock_ahead", request_digest=digest, last_sequence=state.last_sequence)

    if request.device_id != state.device_id or request.pairing_id != state.pairing_id:
        return _result(PairingIngressStatus.BLOCKED, "pairing_identity_mismatch", request_digest=digest, last_sequence=state.last_sequence)
    if state.lifecycle is PairingLifecycleState.REVOKED:
        return _result(PairingIngressStatus.REVOKED, "pairing_revoked", request_digest=digest, last_sequence=state.last_sequence)
    if state.lifecycle is PairingLifecycleState.EXPIRED:
        return _result(PairingIngressStatus.EXPIRED, "pairing_expired", request_digest=digest, last_sequence=state.last_sequence)
    if state.lifecycle is not PairingLifecycleState.PAIRED or not _is_fingerprint(state.credential_fingerprint):
        return _result(PairingIngressStatus.BLOCKED, "pairing_not_active", request_digest=digest, last_sequence=state.last_sequence)
    if state.expires_at is not None:
        state_expiry = _utc_datetime(state.expires_at)
        if state_expiry is None:
            return _result(PairingIngressStatus.BLOCKED, "invalid_pairing_expiry", request_digest=digest, last_sequence=state.last_sequence)
        if current >= state_expiry:
            return _result(PairingIngressStatus.EXPIRED, "pairing_expired", request_digest=digest, last_sequence=state.last_sequence)
    if state.credential_scope_digest is not None and state.credential_scope_digest != _scope_digest(request.scope_key):
        return _result(PairingIngressStatus.BLOCKED, "credential_scope_mismatch", request_digest=digest, last_sequence=state.last_sequence)
    if hmac.compare_digest(request.credential_fingerprint, state.credential_fingerprint):
        pass
    elif any(
        _is_fingerprint(retired) and hmac.compare_digest(request.credential_fingerprint, retired)
        for retired in state.retired_credential_fingerprints
    ):
        return _result(PairingIngressStatus.REVOKED, "credential_rotated_or_revoked", request_digest=digest, last_sequence=state.last_sequence)
    else:
        return _result(PairingIngressStatus.BLOCKED, "credential_fingerprint_mismatch", request_digest=digest, last_sequence=state.last_sequence)

    for entry in state.replay_entries:
        if entry.request_id == request.request_id:
            if _record_matches(entry, request, digest):
                return _result(PairingIngressStatus.DUPLICATE, "request_already_accepted", request_digest=digest, last_sequence=state.last_sequence)
            return _result(PairingIngressStatus.BLOCKED, "request_id_conflict", request_digest=digest, last_sequence=state.last_sequence)
        if hmac.compare_digest(entry.request_digest, digest):
            return _result(PairingIngressStatus.DUPLICATE, "request_digest_already_accepted", request_digest=digest, last_sequence=state.last_sequence)
    if request.sequence <= state.last_sequence:
        return _result(PairingIngressStatus.OUT_OF_ORDER, "sequence_not_monotonic", request_digest=digest, last_sequence=state.last_sequence)
    return _result(PairingIngressStatus.ACCEPTED, "ingress_accepted", request_digest=digest, last_sequence=state.last_sequence)


def record_pairing_acceptance(
    state: NodePairingState,
    request: NodePairingRequest,
    *,
    accepted_at: datetime | None = None,
    policy: NodePairingPolicy | None = None,
) -> NodePairingState:
    """Return the next state after an already accepted request.

    The function revalidates the request so a stale result cannot advance a
    newer snapshot.  It raises ``ValueError`` for a non-accepted request; a
    transport should use :func:`ingest_pairing_request` when it needs a typed
    result instead.
    """

    effective_policy = policy if policy is not None else NodePairingPolicy()
    timestamp = _now_or_utc(accepted_at)
    if timestamp is None:
        raise ValueError("accepted_at must be timezone-aware")
    result = validate_pairing_request(request, state, effective_policy, now=timestamp)
    if not result.accepted:
        raise ValueError(f"cannot record non-accepted pairing request: {result.reason_code}")
    digest = result.request_digest
    assert digest is not None
    entries = list(state.replay_entries)
    entries.append(
        PairingReplayEntry(
            request_id=request.request_id,
            sequence=request.sequence,
            request_digest=digest,
            accepted_at=timestamp,
        )
    )
    entries = entries[-effective_policy.replay_window :]
    return replace(
        state,
        last_sequence=request.sequence,
        replay_entries=tuple(entries),
    )


def ingest_pairing_request(
    state: NodePairingState,
    request: NodePairingRequest,
    policy: NodePairingPolicy | None = None,
    *,
    now: datetime | None = None,
) -> PairingIngressOutcome:
    """Validate and, only on acceptance, return the advanced state snapshot."""

    effective_policy = policy if policy is not None else NodePairingPolicy()
    result = validate_pairing_request(request, state, effective_policy, now=now)
    if not result.accepted:
        return PairingIngressOutcome(result=result, state=state)
    current = _now_or_utc(now)
    assert current is not None
    return PairingIngressOutcome(
        result=result,
        state=record_pairing_acceptance(state, request, accepted_at=current, policy=effective_policy),
    )


def _transition_result(
    status: PairingIngressStatus,
    reason_code: str,
    state: NodePairingState,
) -> PairingTransitionResult:
    return PairingTransitionResult(
        status=status,
        reason_code=reason_code,
        accepted=status is PairingIngressStatus.ACCEPTED,
        state=state,
    )


def _retire_fingerprint(state: NodePairingState, fingerprint: str | None) -> tuple[str, ...]:
    if not _is_fingerprint(fingerprint):
        return state.retired_credential_fingerprints
    values = [fingerprint, *state.retired_credential_fingerprints]
    return tuple(dict.fromkeys(values))[:_MAX_RETIRED_FINGERPRINTS]


def apply_pairing_transition(
    state: NodePairingState,
    action: PairingLifecycleAction | str,
    *,
    now: datetime | None = None,
    current_credential_fingerprint: str | None = None,
    new_credential_fingerprint: str | None = None,
    credential_scope: str | None = None,
    policy: NodePairingPolicy | None = None,
) -> PairingTransitionResult:
    """Apply one deterministic lifecycle transition to an immutable snapshot.

    ``revoke`` and ``expire`` may be invoked by the already-authorized local
    operator without a credential.  ``rotate`` requires the active fingerprint
    and a new derived fingerprint.  No action transition grants node action
    authority.
    """

    if not isinstance(state, NodePairingState):
        raise TypeError("state must be a NodePairingState")
    effective_policy = policy if policy is not None else NodePairingPolicy()
    current = _now_or_utc(now)
    if current is None:
        return _transition_result(PairingIngressStatus.BLOCKED, "invalid_transition_clock", state)
    try:
        transition = action if isinstance(action, PairingLifecycleAction) else PairingLifecycleAction(str(action))
    except (TypeError, ValueError):
        return _transition_result(PairingIngressStatus.BLOCKED, "unknown_pairing_transition", state)
    if not _valid_policy(effective_policy):
        return _transition_result(PairingIngressStatus.BLOCKED, "invalid_policy", state)
    if not _valid_state_shape(state, effective_policy):
        return _transition_result(PairingIngressStatus.BLOCKED, "invalid_pairing_state", state)
    if not _is_identifier(state.device_id) or not _is_identifier(state.pairing_id):
        return _transition_result(PairingIngressStatus.BLOCKED, "invalid_pairing_identity", state)

    if transition is PairingLifecycleAction.PAIR:
        if state.lifecycle is PairingLifecycleState.PAIRED:
            return _transition_result(PairingIngressStatus.BLOCKED, "already_paired", state)
        if state.lifecycle is PairingLifecycleState.REVOKED:
            return _transition_result(PairingIngressStatus.BLOCKED, "revoked_pairing_requires_new_identity", state)
        if not _is_fingerprint(new_credential_fingerprint):
            return _transition_result(PairingIngressStatus.BLOCKED, "invalid_new_credential_fingerprint", state)
        scope_digest = None
        if credential_scope is not None:
            if not isinstance(credential_scope, str) or not credential_scope or len(credential_scope.encode("utf-8")) > _MAX_SCOPE_BYTES:
                return _transition_result(PairingIngressStatus.BLOCKED, "invalid_credential_scope", state)
            scope_digest = _scope_digest(credential_scope)
        updated = replace(
            state,
            lifecycle=PairingLifecycleState.PAIRED,
            credential_fingerprint=new_credential_fingerprint,
            credential_scope_digest=scope_digest,
            policy_version=effective_policy.policy_version,
            last_sequence=0,
            replay_entries=(),
            paired_at=current,
            expires_at=None,
            revoked_at=None,
            generation=state.generation + 1,
        )
        return _transition_result(PairingIngressStatus.ACCEPTED, "pairing_created", updated)

    if transition is PairingLifecycleAction.ROTATE:
        if state.lifecycle is PairingLifecycleState.REVOKED:
            return _transition_result(PairingIngressStatus.REVOKED, "pairing_revoked", state)
        if state.lifecycle is not PairingLifecycleState.PAIRED:
            return _transition_result(PairingIngressStatus.BLOCKED, "pairing_not_active", state)
        if not _is_fingerprint(current_credential_fingerprint) or not _is_fingerprint(new_credential_fingerprint):
            return _transition_result(PairingIngressStatus.BLOCKED, "invalid_rotation_fingerprint", state)
        if not hmac.compare_digest(current_credential_fingerprint, state.credential_fingerprint or ""):
            return _transition_result(PairingIngressStatus.REVOKED, "current_credential_is_not_active", state)
        if hmac.compare_digest(current_credential_fingerprint, new_credential_fingerprint):
            return _transition_result(PairingIngressStatus.BLOCKED, "rotation_requires_new_fingerprint", state)
        scope_digest = state.credential_scope_digest
        if credential_scope is not None:
            if not isinstance(credential_scope, str) or not credential_scope or len(credential_scope.encode("utf-8")) > _MAX_SCOPE_BYTES:
                return _transition_result(PairingIngressStatus.BLOCKED, "invalid_credential_scope", state)
            scope_digest = _scope_digest(credential_scope)
        updated = replace(
            state,
            credential_fingerprint=new_credential_fingerprint,
            credential_scope_digest=scope_digest,
            retired_credential_fingerprints=_retire_fingerprint(state, current_credential_fingerprint),
            generation=state.generation + 1,
        )
        return _transition_result(PairingIngressStatus.ACCEPTED, "credential_rotated", updated)

    if transition is PairingLifecycleAction.REVOKE:
        if state.lifecycle is PairingLifecycleState.REVOKED:
            return _transition_result(PairingIngressStatus.REVOKED, "pairing_already_revoked", state)
        if current_credential_fingerprint is not None and (
            not _is_fingerprint(current_credential_fingerprint)
            or not hmac.compare_digest(current_credential_fingerprint, state.credential_fingerprint or "")
        ):
            return _transition_result(PairingIngressStatus.BLOCKED, "current_credential_is_not_active", state)
        updated = replace(
            state,
            lifecycle=PairingLifecycleState.REVOKED,
            credential_fingerprint=None,
            retired_credential_fingerprints=_retire_fingerprint(state, state.credential_fingerprint),
            revoked_at=current,
            generation=state.generation + 1,
        )
        return _transition_result(PairingIngressStatus.ACCEPTED, "pairing_revoked", updated)

    if state.lifecycle is PairingLifecycleState.REVOKED:
        return _transition_result(PairingIngressStatus.REVOKED, "pairing_revoked", state)
    if state.lifecycle is PairingLifecycleState.EXPIRED:
        return _transition_result(PairingIngressStatus.EXPIRED, "pairing_already_expired", state)
    if state.lifecycle is not PairingLifecycleState.PAIRED:
        return _transition_result(PairingIngressStatus.BLOCKED, "pairing_not_active", state)
    updated = replace(
        state,
        lifecycle=PairingLifecycleState.EXPIRED,
        credential_fingerprint=None,
        retired_credential_fingerprints=_retire_fingerprint(state, state.credential_fingerprint),
        expires_at=current,
        generation=state.generation + 1,
    )
    return _transition_result(PairingIngressStatus.ACCEPTED, "pairing_expired", updated)


def _redacted_path(value: str | None) -> str | None:
    return "[redacted path]" if value else None


def _fingerprint_receipt_value(value: str | None) -> str | None:
    if not _is_fingerprint(value):
        return None
    return "[redacted fingerprint]"


def serialize_pairing_receipt(
    request: NodePairingRequest | None = None,
    result: PairingValidationResult | PairingTransitionResult | None = None,
    *,
    state: NodePairingState | None = None,
) -> dict[str, Any]:
    """Build an operator-safe metadata receipt.

    Raw payloads, source paths, raw tokens, and full credential fingerprints
    are never returned.  Content hashes and request digests are identifiers for
    verification/readback and do not contain the captured content.
    """

    status = result.status if result is not None else PairingIngressStatus.BLOCKED
    reason_code = result.reason_code if result is not None else "no_result"
    accepted = bool(result.accepted) if result is not None else False
    retryable = bool(result.retryable) if result is not None else False
    request_digest = None
    last_sequence = None
    if isinstance(result, PairingValidationResult):
        request_digest = result.request_digest
        last_sequence = result.last_sequence
    elif isinstance(result, PairingTransitionResult):
        state = result.state
    receipt: dict[str, Any] = {
        "schema": NODE_PAIRING_RECEIPT_SCHEMA_VERSION,
        "status": status.value,
        "accepted": accepted,
        "retryable": retryable,
        "terminal": not retryable,
        "reason_code": reason_code,
        "request_digest": request_digest,
        "policy_version": request.policy_version if request is not None else state.policy_version if state else None,
        "identity": {
            "device_id": request.device_id if request is not None else state.device_id if state else None,
            "pairing_id": request.pairing_id if request is not None else state.pairing_id if state else None,
            "request_id": request.request_id if request is not None else None,
        },
        "capture": {
            "sequence": request.sequence if request is not None else None,
            "last_sequence": last_sequence if last_sequence is not None else state.last_sequence if state else None,
            "captured_at": _iso(request.captured_at) if request is not None else None,
            "content_hash": _normalized_content_hash(request.content_hash) if request is not None else None,
            "media_type": request.media_type if request is not None else None,
            "size_bytes": request.content_size if request is not None else None,
            "source_path": _redacted_path(request.source_path) if request is not None else None,
            "content_redacted": True,
        },
        "scope": {
            "capability": request.capability_scope if request is not None else None,
            "data_purpose": request.data_purpose if request is not None else None,
            "action_authority": bool(request.action_authority) if request is not None else False,
            "credential_fingerprint": _fingerprint_receipt_value(
                request.credential_fingerprint if request is not None else state.credential_fingerprint if state else None
            ),
        },
        "lifecycle": {
            "state": state.lifecycle.value if state is not None else None,
            "generation": state.generation if state is not None else None,
            "paired_at": _iso(state.paired_at) if state is not None else None,
            "expires_at": _iso(state.expires_at) if state is not None else None,
            "revoked_at": _iso(state.revoked_at) if state is not None else None,
            "retired_credential_count": len(state.retired_credential_fingerprints) if state is not None else None,
        },
        "transport": {
            "authentication_claimed": False,
            "live_mac_transport_claimed": False,
            "artifact_readback_claimed": False,
        },
    }
    return receipt


def serialize_transition_receipt(
    transition: PairingTransitionResult,
    *,
    state_before: NodePairingState | None = None,
) -> dict[str, Any]:
    """Serialize a lifecycle transition without exposing old credentials."""

    receipt = serialize_pairing_receipt(result=transition, state=transition.state)
    receipt["transition"] = {
        "status": transition.status.value,
        "reason_code": transition.reason_code,
        "previous_state": state_before.lifecycle.value if state_before is not None else None,
        "state": transition.state.lifecycle.value,
    }
    return receipt


__all__ = [
    "DEFAULT_ALLOWED_CAPABILITIES",
    "DEFAULT_ALLOWED_DATA_PURPOSES",
    "DEFAULT_ALLOWED_MEDIA_TYPES",
    "DEFAULT_POLICY_VERSION",
    "NodePairingPolicy",
    "NodePairingRequest",
    "NodePairingState",
    "PairingIngressOutcome",
    "PairingIngressStatus",
    "PairingLifecycleAction",
    "PairingLifecycleState",
    "PairingPolicy",
    "PairingReplayEntry",
    "PairingRequest",
    "PairingState",
    "PairingTransitionResult",
    "PairingValidationResult",
    "apply_pairing_transition",
    "canonical_request_digest",
    "ingest_pairing_request",
    "record_pairing_acceptance",
    "scoped_credential_fingerprint",
    "serialize_pairing_receipt",
    "serialize_transition_receipt",
    "validate_pairing_request",
]
