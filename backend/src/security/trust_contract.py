"""Versioned, provider-neutral trust decisions for Seraph runtime boundaries.

The contract deliberately accepts digests and references rather than content or
credential values.  Callers retain ownership of payload storage and transport;
this module only decides whether the declared boundary is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
import time
from typing import Iterable
from uuid import uuid4


TRUST_SCHEMA_VERSION = "seraph.trust.v1"


class EgressClass(str, Enum):
    """Maximum destination class allowed for a piece of data."""

    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED_REDACTED = "cloud_allowed_redacted"
    CLOUD_ALLOWED_FULL = "cloud_allowed_full"


class ContentOrigin(str, Enum):
    SERAPH_CONTROL = "seraph_control"
    OPERATOR_INPUT = "operator_input"
    CANONICAL_MEMORY = "canonical_memory"
    PAIRED_EDGE = "paired_edge"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    PROVIDER_OUTPUT = "provider_output"


class PrincipalType(str, Enum):
    OPERATOR = "operator"
    SERAPH_RUNTIME = "seraph_runtime"
    PAIRED_EDGE = "paired_edge"
    SERVICE = "service"
    ANONYMOUS = "anonymous"


class AuthorityGrant(str, Enum):
    INGRESS = "ingress"
    MODEL_INFERENCE = "model_inference"
    CAPABILITY_EXECUTE = "capability_execute"
    ARTIFACT_TRANSFER = "artifact_transfer"
    CREDENTIAL_EGRESS = "credential_egress"
    EXTERNAL_MUTATION = "external_mutation"


class DigestSentinel(str, Enum):
    NO_SECRET_SCOPE = "no_secret_scope"
    NO_RESOURCE_LIMITS = "no_resource_limits"
    NO_TRANSFORMATION = "no_transformation"
    NO_OBJECT = "no_object"


class DestinationClass(str, Enum):
    LOCAL_RUNTIME = "local_runtime"
    TRUSTED_LAN_RUNTIME = "trusted_lan_runtime"
    REMOTE_PROVIDER = "remote_provider"
    MANAGED_CONNECTOR = "managed_connector"
    EXTERNAL_SYSTEM = "external_system"


class TrustOperation(str, Enum):
    INGRESS = "ingress"
    MODEL_INFERENCE = "model_inference"
    CAPABILITY_CALL = "capability_call"
    ARTIFACT_TRANSFER = "artifact_transfer"
    CREDENTIAL_EGRESS = "credential_egress"
    EXTERNAL_MUTATION = "external_mutation"


class DecisionEffect(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class RecoveryClass(str, Enum):
    NONE = "none"
    RETRY = "retry"
    RESUME = "resume"
    COMPENSATE = "compensate"
    QUARANTINE = "quarantine"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class TrustPrincipal:
    principal_id: str
    principal_type: PrincipalType | str
    authenticated: bool = True
    revoked: bool = False
    grants: tuple[AuthorityGrant | str, ...] = ()
    session_id: str = ""
    job_id: str = ""
    # ``session_id`` is the execution/conversation scope used by the trust
    # contract.  Authenticated operator sessions are a separate identity
    # binding so a browser session cannot be confused with a conversation id.
    operator_session_id: str = ""


@dataclass(frozen=True)
class TrustProvenance:
    origin: ContentOrigin | str
    source_id: str
    data_digest: str
    egress_class: EgressClass | str
    instruction_authority: bool = False


@dataclass(frozen=True)
class TrustDestination:
    destination_id: str
    destination_class: DestinationClass | str
    endpoint: str = ""


@dataclass(frozen=True)
class TrustResource:
    resource_type: str
    resource_id: str
    object_digest: str | DigestSentinel


@dataclass(frozen=True)
class ApprovalBinding:
    approval_id: str
    request_digest: str
    policy_version: str
    destination_digest: str
    capability_id: str
    capability_version: str
    data_digest: str
    secret_scope_digest: str
    resource_limits_digest: str
    transformation_digest: str
    authority_scope_digest: str
    resource_digest: str
    session_id: str
    job_id: str
    request_id: str
    attempt_id: str
    replay_id: str
    decision_expires_at: float
    expires_at: float
    consumed: bool = False


@dataclass(frozen=True)
class AuditBinding:
    receipt_id: str = ""
    required: bool = False
    persisted: bool = False
    durable: bool = False


@dataclass(frozen=True)
class RecoveryBinding:
    recovery_class: RecoveryClass | str = RecoveryClass.NONE
    checkpoint_id: str = ""
    required: bool = False


@dataclass(frozen=True)
class TrustRequest:
    principal: TrustPrincipal
    provenance: tuple[TrustProvenance, ...]
    destination: TrustDestination
    operation: TrustOperation | str
    required_grant: AuthorityGrant | str
    capability_id: str
    capability_version: str
    data_digest: str
    secret_scope_digest: str | DigestSentinel
    resource_limits_digest: str | DigestSentinel
    transformation_digest: str | DigestSentinel
    authority_scope_digest: str
    resource: TrustResource
    session_id: str
    job_id: str
    request_id: str
    attempt_id: str
    replay_id: str
    decision_expires_at: float
    egress_class: EgressClass | str
    policy_version: str = TRUST_SCHEMA_VERSION
    redaction_applied: bool = False
    approval_required: bool = False
    approval: ApprovalBinding | None = None
    audit: AuditBinding = field(default_factory=AuditBinding)
    recovery: RecoveryBinding = field(default_factory=RecoveryBinding)


@dataclass(frozen=True)
class TrustDecision:
    schema_version: str
    effect: DecisionEffect
    reason_code: str
    request_digest: str
    decision_id: str
    effective_egress_class: str
    destination_class: str
    decision_expires_at: float

    @property
    def allowed(self) -> bool:
        return self.effect is DecisionEffect.ALLOW

    def as_dict(self) -> dict[str, str | bool | float]:
        return {
            "schema_version": self.schema_version,
            "effect": self.effect.value,
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "decision_id": self.decision_id,
            "effective_egress_class": self.effective_egress_class,
            "destination_class": self.destination_class,
            "decision_expires_at": self.decision_expires_at,
        }


_EGRESS_RANK = {
    EgressClass.LOCAL_ONLY: 0,
    EgressClass.CLOUD_ALLOWED_REDACTED: 1,
    EgressClass.CLOUD_ALLOWED_FULL: 2,
}
_LOCAL_DESTINATIONS = {
    DestinationClass.LOCAL_RUNTIME,
    DestinationClass.TRUSTED_LAN_RUNTIME,
}
_UNTRUSTED_ORIGINS = {
    ContentOrigin.EXTERNAL_UNTRUSTED,
    ContentOrigin.PROVIDER_OUTPUT,
}
_GRANT_BY_OPERATION = {
    TrustOperation.INGRESS: AuthorityGrant.INGRESS,
    TrustOperation.MODEL_INFERENCE: AuthorityGrant.MODEL_INFERENCE,
    TrustOperation.CAPABILITY_CALL: AuthorityGrant.CAPABILITY_EXECUTE,
    TrustOperation.ARTIFACT_TRANSFER: AuthorityGrant.ARTIFACT_TRANSFER,
    TrustOperation.CREDENTIAL_EGRESS: AuthorityGrant.CREDENTIAL_EGRESS,
    TrustOperation.EXTERNAL_MUTATION: AuthorityGrant.EXTERNAL_MUTATION,
}
_PAIRED_EDGE_OPERATIONS = {TrustOperation.INGRESS, TrustOperation.ARTIFACT_TRANSFER}
_PRIVILEGED_OPERATIONS = {
    TrustOperation.INGRESS,
    TrustOperation.MODEL_INFERENCE,
    TrustOperation.CAPABILITY_CALL,
    TrustOperation.ARTIFACT_TRANSFER,
    TrustOperation.CREDENTIAL_EGRESS,
    TrustOperation.EXTERNAL_MUTATION,
}
_NON_DISCRETIONARY_APPROVAL_OPERATIONS = {
    TrustOperation.CREDENTIAL_EGRESS,
    TrustOperation.EXTERNAL_MUTATION,
}
_NON_DISCRETIONARY_AUDIT_OPERATIONS = _NON_DISCRETIONARY_APPROVAL_OPERATIONS
_NON_DISCRETIONARY_RECOVERY_OPERATIONS = _NON_DISCRETIONARY_APPROVAL_OPERATIONS
NO_SECRET_SCOPE = DigestSentinel.NO_SECRET_SCOPE
NO_RESOURCE_LIMITS = DigestSentinel.NO_RESOURCE_LIMITS
NO_TRANSFORMATION = DigestSentinel.NO_TRANSFORMATION
NO_OBJECT = DigestSentinel.NO_OBJECT
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_DECISION_TTL_SECONDS = 300.0

_PRINCIPAL_OPERATIONS = {
    PrincipalType.OPERATOR: frozenset(TrustOperation),
    PrincipalType.SERAPH_RUNTIME: frozenset(
        {
            TrustOperation.MODEL_INFERENCE,
            TrustOperation.CAPABILITY_CALL,
            TrustOperation.ARTIFACT_TRANSFER,
        }
    ),
    PrincipalType.SERVICE: frozenset(
        {
            TrustOperation.MODEL_INFERENCE,
            TrustOperation.CAPABILITY_CALL,
            TrustOperation.ARTIFACT_TRANSFER,
        }
    ),
    PrincipalType.PAIRED_EDGE: frozenset(_PAIRED_EDGE_OPERATIONS),
    PrincipalType.ANONYMOUS: frozenset(),
}


def canonical_digest(payload: object) -> str:
    """Return the canonical lowercase SHA-256 digest for redacted metadata."""
    return _sha256_json(payload)


def is_canonical_digest(value: object, *, sentinels: Iterable[DigestSentinel] = ()) -> bool:
    if isinstance(value, DigestSentinel):
        return value in frozenset(sentinels)
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_reference(value: object) -> bool:
    return isinstance(value, str) and _REFERENCE_RE.fullmatch(value) is not None


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _parse_enum(enum_type: type[Enum], value: Enum | str) -> Enum | None:
    try:
        return enum_type(_enum_value(value))
    except (TypeError, ValueError):
        return None


def _sha256_json(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def destination_digest(destination: TrustDestination) -> str:
    """Return a stable destination binding without including request content."""
    return _sha256_json(
        {
            "destination_id": destination.destination_id,
            "destination_class": _enum_value(destination.destination_class),
            "endpoint": destination.endpoint,
        }
    )


def authority_scope_digest(
    *,
    required_grant: AuthorityGrant | str,
    capability_id: str,
    destination: TrustDestination,
    resource: TrustResource,
) -> str:
    """Bind a grant to one capability, destination, and resource target."""
    return canonical_digest(
        {
            "required_grant": _enum_value(required_grant),
            "capability_id": capability_id,
            "destination_digest": destination_digest(destination),
            "resource": {
                "resource_type": resource.resource_type,
                "resource_id": resource.resource_id,
                "object_digest": resource.object_digest,
            },
        }
    )


def effective_egress_class(provenance: Iterable[TrustProvenance]) -> EgressClass | None:
    """Join provenance monotonically, choosing the most restrictive class."""
    parsed: list[EgressClass] = []
    for item in provenance:
        value = _parse_enum(EgressClass, item.egress_class)
        if not isinstance(value, EgressClass):
            return None
        parsed.append(value)
    if not parsed:
        return None
    return min(parsed, key=_EGRESS_RANK.__getitem__)


def principal_operation_reason(
    *,
    principal: TrustPrincipal,
    operation: TrustOperation | str,
    required_grant: AuthorityGrant | str,
) -> str | None:
    """Return the fail-closed principal/operation authorization reason, if any."""
    if not principal.principal_id or not principal.principal_type:
        return "principal_missing"
    parsed_operation = _parse_enum(TrustOperation, operation)
    if not isinstance(parsed_operation, TrustOperation):
        return "operation_unknown"
    if principal.revoked:
        return "principal_revoked"
    principal_type = _parse_enum(PrincipalType, principal.principal_type)
    if (
        not isinstance(principal_type, PrincipalType)
        or principal_type is PrincipalType.ANONYMOUS
        or not principal.authenticated
    ) and parsed_operation in _PRIVILEGED_OPERATIONS:
        return "principal_unauthorized"
    if parsed_operation not in _PRINCIPAL_OPERATIONS[principal_type]:
        return "principal_operation_forbidden"
    expected_grant = _GRANT_BY_OPERATION[parsed_operation]
    parsed_required_grant = _parse_enum(AuthorityGrant, required_grant)
    grants = {_parse_enum(AuthorityGrant, grant) for grant in principal.grants}
    if parsed_required_grant is not expected_grant:
        return "required_grant_invalid"
    if expected_grant not in grants:
        return "authority_grant_missing"
    return None


def trust_request_digest(request: TrustRequest) -> str:
    """Digest only redacted trust metadata; approval state is intentionally excluded."""
    return _sha256_json(
        {
            "schema_version": request.policy_version,
            "principal": {
                "id": request.principal.principal_id,
                "type": _enum_value(request.principal.principal_type),
                "authenticated": request.principal.authenticated,
                "revoked": request.principal.revoked,
                "grants": sorted(_enum_value(grant) for grant in request.principal.grants),
                "session_id": request.principal.session_id,
                "job_id": request.principal.job_id,
            },
            "provenance": [
                {
                    "origin": _enum_value(item.origin),
                    "source_id": item.source_id,
                    "data_digest": item.data_digest,
                    "egress_class": _enum_value(item.egress_class),
                    "instruction_authority": item.instruction_authority,
                }
                for item in request.provenance
            ],
            "destination_digest": destination_digest(request.destination),
            "operation": _enum_value(request.operation),
            "required_grant": _enum_value(request.required_grant),
            "capability_id": request.capability_id,
            "capability_version": request.capability_version,
            "data_digest": request.data_digest,
            "secret_scope_digest": request.secret_scope_digest,
            "resource_limits_digest": request.resource_limits_digest,
            "transformation_digest": request.transformation_digest,
            "authority_scope_digest": request.authority_scope_digest,
            "resource": {
                "resource_type": request.resource.resource_type,
                "resource_id": request.resource.resource_id,
                "object_digest": request.resource.object_digest,
            },
            "session_id": request.session_id,
            "job_id": request.job_id,
            "request_id": request.request_id,
            "attempt_id": request.attempt_id,
            "replay_id": request.replay_id,
            "decision_expires_at": request.decision_expires_at,
            "egress_class": _enum_value(request.egress_class),
            "redaction_applied": request.redaction_applied,
            "approval_required": request.approval_required,
            "audit": {
                "receipt_id": request.audit.receipt_id,
                "required": request.audit.required,
                "persisted": request.audit.persisted,
                "durable": request.audit.durable,
            },
            "recovery": {
                "recovery_class": _enum_value(request.recovery.recovery_class),
                "checkpoint_id": request.recovery.checkpoint_id,
                "required": request.recovery.required,
            },
        }
    )


def bind_approval(request: TrustRequest, *, approval_id: str, expires_at: float) -> ApprovalBinding:
    """Create an exact, expiring approval binding for a request."""
    return ApprovalBinding(
        approval_id=approval_id,
        request_digest=trust_request_digest(request),
        policy_version=request.policy_version,
        destination_digest=destination_digest(request.destination),
        capability_id=request.capability_id,
        capability_version=request.capability_version,
        data_digest=request.data_digest,
        secret_scope_digest=request.secret_scope_digest,
        resource_limits_digest=request.resource_limits_digest,
        transformation_digest=request.transformation_digest,
        authority_scope_digest=request.authority_scope_digest,
        resource_digest=canonical_digest(
            {
                "resource_type": request.resource.resource_type,
                "resource_id": request.resource.resource_id,
                "object_digest": request.resource.object_digest,
            }
        ),
        session_id=request.session_id,
        job_id=request.job_id,
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        replay_id=request.replay_id,
        decision_expires_at=request.decision_expires_at,
        expires_at=float(expires_at),
    )


def _decision(request: TrustRequest, effect: DecisionEffect, reason_code: str) -> TrustDecision:
    request_id = trust_request_digest(request)
    effective = effective_egress_class(request.provenance)
    destination_class = _enum_value(request.destination.destination_class)
    decision_id = f"trust_{_sha256_json({'request': request_id, 'effect': effect.value, 'reason': reason_code})[:24]}"
    return TrustDecision(
        schema_version=TRUST_SCHEMA_VERSION,
        effect=effect,
        reason_code=reason_code,
        request_digest=request_id,
        decision_id=decision_id,
        effective_egress_class=effective.value if effective is not None else "unknown",
        destination_class=destination_class or "unknown",
        decision_expires_at=request.decision_expires_at,
    )


def _approval_reason(
    request: TrustRequest,
    *,
    now: float,
    replayed_approval_ids: frozenset[str],
) -> str | None:
    binding = request.approval
    if binding is None:
        return "approval_missing"
    if not binding.approval_id:
        return "approval_id_missing"
    if binding.approval_id in replayed_approval_ids or binding.consumed:
        return "approval_replayed"
    if binding.expires_at <= now:
        return "approval_expired"
    if binding.policy_version != request.policy_version:
        return "approval_policy_drift"
    if binding.destination_digest != destination_digest(request.destination):
        return "approval_destination_drift"
    if binding.capability_id != request.capability_id:
        return "approval_capability_drift"
    if binding.capability_version != request.capability_version:
        return "approval_capability_version_drift"
    if binding.data_digest != request.data_digest:
        return "approval_data_drift"
    if binding.secret_scope_digest != request.secret_scope_digest:
        return "approval_secret_scope_drift"
    if binding.resource_limits_digest != request.resource_limits_digest:
        return "approval_resource_limits_drift"
    if binding.transformation_digest != request.transformation_digest:
        return "approval_transformation_drift"
    if binding.authority_scope_digest != request.authority_scope_digest:
        return "approval_authority_scope_drift"
    resource_digest = canonical_digest(
        {
            "resource_type": request.resource.resource_type,
            "resource_id": request.resource.resource_id,
            "object_digest": request.resource.object_digest,
        }
    )
    if binding.resource_digest != resource_digest:
        return "approval_resource_drift"
    if binding.session_id != request.session_id:
        return "approval_session_drift"
    if binding.job_id != request.job_id:
        return "approval_job_drift"
    if binding.request_id != request.request_id:
        return "approval_request_id_drift"
    if binding.attempt_id != request.attempt_id:
        return "approval_attempt_drift"
    if binding.replay_id != request.replay_id:
        return "approval_replay_drift"
    if binding.decision_expires_at != request.decision_expires_at:
        return "approval_decision_expiry_drift"
    if binding.request_digest != trust_request_digest(request):
        return "approval_request_drift"
    return None


def evaluate_trust(
    request: TrustRequest,
    *,
    now: float | None = None,
    replayed_approval_ids: Iterable[str] = (),
    replayed_attempt_ids: Iterable[str] = (),
    replayed_replay_ids: Iterable[str] = (),
    verified_audit_receipt_ids: Iterable[str] = (),
) -> TrustDecision:
    """Evaluate a request without inspecting or returning its raw payload."""
    evaluated_at = time.time() if now is None else float(now)
    if request.policy_version != TRUST_SCHEMA_VERSION:
        return _decision(request, DecisionEffect.DENY, "unknown_policy_version")
    operation = _parse_enum(TrustOperation, request.operation)
    destination_class = _parse_enum(DestinationClass, request.destination.destination_class)
    requested_egress = _parse_enum(EgressClass, request.egress_class)
    if not isinstance(operation, TrustOperation):
        return _decision(request, DecisionEffect.DENY, "operation_unknown")
    principal_reason = principal_operation_reason(
        principal=request.principal,
        operation=operation,
        required_grant=request.required_grant,
    )
    if principal_reason is not None:
        return _decision(request, DecisionEffect.DENY, principal_reason)
    expected_grant = _GRANT_BY_OPERATION[operation]
    if request.principal.session_id != request.session_id:
        return _decision(request, DecisionEffect.DENY, "principal_session_mismatch")
    if request.principal.job_id != request.job_id:
        return _decision(request, DecisionEffect.DENY, "principal_job_mismatch")
    if not request.session_id and not request.job_id:
        return _decision(request, DecisionEffect.DENY, "execution_identity_missing")
    for reference, reason in (
        (request.request_id, "request_id_invalid"),
        (request.attempt_id, "attempt_id_invalid"),
        (request.replay_id, "replay_id_invalid"),
    ):
        if not _valid_reference(reference):
            return _decision(request, DecisionEffect.DENY, reason)
    if request.attempt_id in frozenset(replayed_attempt_ids) or request.replay_id in frozenset(replayed_replay_ids):
        return _decision(request, DecisionEffect.DENY, "request_replayed")
    if request.decision_expires_at <= evaluated_at:
        return _decision(request, DecisionEffect.DENY, "decision_expired")
    if request.decision_expires_at > evaluated_at + _MAX_DECISION_TTL_SECONDS:
        return _decision(request, DecisionEffect.DENY, "decision_expiry_unbounded")
    if not request.capability_id:
        return _decision(request, DecisionEffect.DENY, "capability_missing")
    if not request.capability_version:
        return _decision(request, DecisionEffect.DENY, "capability_version_missing")
    digest_fields = (
        (request.data_digest, (), "data_digest_invalid"),
        (request.secret_scope_digest, (NO_SECRET_SCOPE,), "secret_scope_digest_invalid"),
        (request.resource_limits_digest, (NO_RESOURCE_LIMITS,), "resource_limits_digest_invalid"),
        (request.transformation_digest, (NO_TRANSFORMATION,), "transformation_digest_invalid"),
        (request.authority_scope_digest, (), "authority_scope_digest_invalid"),
        (request.resource.object_digest, (NO_OBJECT,), "resource_object_digest_invalid"),
    )
    for value, sentinels, reason in digest_fields:
        if not is_canonical_digest(value, sentinels=sentinels):
            return _decision(request, DecisionEffect.DENY, reason)
    if not _valid_reference(request.resource.resource_type) or not _valid_reference(request.resource.resource_id):
        return _decision(request, DecisionEffect.DENY, "resource_target_invalid")
    if operation is TrustOperation.MODEL_INFERENCE and request.secret_scope_digest != NO_SECRET_SCOPE:
        return _decision(request, DecisionEffect.DENY, "secret_scope_forbidden_for_model")
    if not request.destination.destination_id or not isinstance(destination_class, DestinationClass):
        return _decision(request, DecisionEffect.DENY, "destination_unknown")
    if not isinstance(requested_egress, EgressClass):
        return _decision(request, DecisionEffect.DENY, "egress_class_unknown")
    if not request.provenance:
        return _decision(request, DecisionEffect.DENY, "provenance_missing")
    for item in request.provenance:
        origin = _parse_enum(ContentOrigin, item.origin)
        if not isinstance(origin, ContentOrigin) or not _valid_reference(item.source_id):
            return _decision(request, DecisionEffect.DENY, "provenance_invalid")
        if not is_canonical_digest(item.data_digest):
            return _decision(request, DecisionEffect.DENY, "provenance_digest_invalid")
        if origin in _UNTRUSTED_ORIGINS and item.instruction_authority:
            return _decision(request, DecisionEffect.DENY, "untrusted_instruction_authority")

    effective = effective_egress_class(request.provenance)
    if effective is None:
        return _decision(request, DecisionEffect.DENY, "provenance_egress_unknown")
    if _EGRESS_RANK[requested_egress] > _EGRESS_RANK[effective]:
        return _decision(request, DecisionEffect.DENY, "egress_class_widening")
    audit_required = request.audit.required or operation in _NON_DISCRETIONARY_AUDIT_OPERATIONS
    recovery_required = request.recovery.required or operation in _NON_DISCRETIONARY_RECOVERY_OPERATIONS
    approval_required = request.approval_required or operation in _NON_DISCRETIONARY_APPROVAL_OPERATIONS
    if audit_required:
        if (
            not _valid_reference(request.audit.receipt_id)
            or not request.audit.persisted
            or not request.audit.durable
        ):
            return _decision(request, DecisionEffect.DENY, "audit_binding_missing")
        if request.audit.receipt_id not in frozenset(verified_audit_receipt_ids):
            return _decision(request, DecisionEffect.DENY, "audit_receipt_unverified")
    recovery_class = _parse_enum(RecoveryClass, request.recovery.recovery_class)
    if not isinstance(recovery_class, RecoveryClass):
        return _decision(request, DecisionEffect.DENY, "recovery_class_invalid")
    if recovery_required and recovery_class is RecoveryClass.NONE:
        return _decision(request, DecisionEffect.DENY, "recovery_binding_missing")
    if approval_required:
        if request.approval is None:
            return _decision(request, DecisionEffect.REQUIRE_APPROVAL, "approval_missing")
        reason = _approval_reason(
            request,
            now=evaluated_at,
            replayed_approval_ids=frozenset(replayed_approval_ids),
        )
        if reason is not None:
            return _decision(request, DecisionEffect.DENY, reason)

    expected_authority_scope = authority_scope_digest(
        required_grant=expected_grant,
        capability_id=request.capability_id,
        destination=request.destination,
        resource=request.resource,
    )
    if request.authority_scope_digest != expected_authority_scope:
        return _decision(request, DecisionEffect.DENY, "authority_scope_mismatch")
    if operation is TrustOperation.MODEL_INFERENCE and (
        request.resource.resource_type != "model_endpoint"
        or request.resource.resource_id != request.destination.destination_id
    ):
        return _decision(request, DecisionEffect.DENY, "resource_target_mismatch")

    if destination_class not in _LOCAL_DESTINATIONS:
        if requested_egress is EgressClass.LOCAL_ONLY:
            return _decision(request, DecisionEffect.DENY, "local_only_egress_blocked")
        if requested_egress is EgressClass.CLOUD_ALLOWED_REDACTED and not request.redaction_applied:
            return _decision(request, DecisionEffect.DENY, "cloud_redaction_required")
        if requested_egress is EgressClass.CLOUD_ALLOWED_REDACTED and request.transformation_digest is NO_TRANSFORMATION:
            return _decision(request, DecisionEffect.DENY, "transformation_binding_missing")

    return _decision(request, DecisionEffect.ALLOW, "policy_allowed")


def strict_local_inference_request(
    *,
    principal_id: str,
    capability_id: str,
    data_digest: str,
    destination: TrustDestination,
    source_id: str,
) -> TrustRequest:
    """Build the narrow request used by existing strict-local inference paths."""
    attempt_token = uuid4().hex
    return TrustRequest(
        principal=TrustPrincipal(
            principal_id=principal_id,
            principal_type=PrincipalType.SERAPH_RUNTIME,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            job_id=f"job:{source_id}",
        ),
        provenance=(
            TrustProvenance(
                origin=ContentOrigin.OPERATOR_INPUT,
                source_id=source_id,
                data_digest=data_digest,
                egress_class=EgressClass.LOCAL_ONLY,
            ),
        ),
        destination=destination,
        operation=TrustOperation.MODEL_INFERENCE,
        required_grant=AuthorityGrant.MODEL_INFERENCE,
        capability_id=capability_id,
        capability_version="legacy-local-runtime-v1",
        data_digest=data_digest,
        secret_scope_digest=NO_SECRET_SCOPE,
        resource_limits_digest=NO_RESOURCE_LIMITS,
        transformation_digest=NO_TRANSFORMATION,
        authority_scope_digest=authority_scope_digest(
            required_grant=AuthorityGrant.MODEL_INFERENCE,
            capability_id=capability_id,
            destination=destination,
            resource=TrustResource(
                resource_type="model_endpoint",
                resource_id=destination.destination_id,
                object_digest=NO_OBJECT,
            ),
        ),
        resource=TrustResource(
            resource_type="model_endpoint",
            resource_id=destination.destination_id,
            object_digest=NO_OBJECT,
        ),
        session_id="",
        job_id=f"job:{source_id}",
        request_id=f"request:{attempt_token}",
        attempt_id=f"attempt:{attempt_token}",
        replay_id=f"replay:{attempt_token}",
        decision_expires_at=time.time() + 60.0,
        egress_class=EgressClass.LOCAL_ONLY,
    )
