"""Focused tests for the shared Seraph trust contract."""

from dataclasses import replace
import json

import pytest

from src.security.trust_contract import (
    TRUST_SCHEMA_VERSION,
    ApprovalBinding,
    AuthorityGrant,
    AuditBinding,
    ContentOrigin,
    DestinationClass,
    EgressClass,
    NO_RESOURCE_LIMITS,
    NO_SECRET_SCOPE,
    NO_TRANSFORMATION,
    NO_OBJECT,
    PrincipalType,
    RecoveryBinding,
    RecoveryClass,
    TrustDestination,
    TrustOperation,
    TrustPrincipal,
    TrustProvenance,
    TrustRequest,
    TrustResource,
    authority_scope_digest,
    bind_approval,
    canonical_digest,
    effective_egress_class,
    evaluate_trust as _evaluate_trust,
    principal_operation_reason,
)


def _digest(label: str) -> str:
    return canonical_digest({"label": label})


def evaluate_trust(request: TrustRequest, *, now: float = 100.0, **kwargs):
    return _evaluate_trust(request, now=now, **kwargs)


def _request(
    *,
    egress_class: EgressClass | str = EgressClass.LOCAL_ONLY,
    provenance_egress: EgressClass | str | None = None,
    destination_class: DestinationClass | str = DestinationClass.LOCAL_RUNTIME,
    approval_required: bool = False,
    origin: ContentOrigin | str = ContentOrigin.OPERATOR_INPUT,
    instruction_authority: bool = False,
    redaction_applied: bool = False,
) -> TrustRequest:
    provenance_class = egress_class if provenance_egress is None else provenance_egress
    return TrustRequest(
        principal=TrustPrincipal(
            principal_id="operator-1",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id="session-1",
        ),
        provenance=(
            TrustProvenance(
                origin=origin,
                source_id="message-1",
                data_digest=_digest("data"),
                egress_class=provenance_class,
                instruction_authority=instruction_authority,
            ),
        ),
        destination=TrustDestination(
            destination_id="runtime-1",
            destination_class=destination_class,
            endpoint="https://models.example/v1",
        ),
        operation=TrustOperation.MODEL_INFERENCE,
        required_grant=AuthorityGrant.MODEL_INFERENCE,
        capability_id="chat_agent",
        capability_version="v1",
        data_digest=_digest("data"),
        secret_scope_digest=NO_SECRET_SCOPE,
        resource_limits_digest=NO_RESOURCE_LIMITS,
        transformation_digest=(_digest("redaction-v1") if redaction_applied else NO_TRANSFORMATION),
        authority_scope_digest=authority_scope_digest(
            required_grant=AuthorityGrant.MODEL_INFERENCE,
            capability_id="chat_agent",
            destination=TrustDestination(
                destination_id="runtime-1",
                destination_class=destination_class,
                endpoint="https://models.example/v1",
            ),
            resource=TrustResource(
                resource_type="model_endpoint",
                resource_id="runtime-1",
                object_digest=NO_OBJECT,
            ),
        ),
        resource=TrustResource(
            resource_type="model_endpoint",
            resource_id="runtime-1",
            object_digest=NO_OBJECT,
        ),
        session_id="session-1",
        job_id="",
        request_id="request-1",
        attempt_id="attempt-1",
        replay_id="replay-1",
        decision_expires_at=150.0,
        egress_class=egress_class,
        approval_required=approval_required,
        redaction_applied=redaction_applied,
    )


def _approved(request: TrustRequest, *, expires_at: float = 200.0) -> TrustRequest:
    return replace(
        request,
        approval=bind_approval(request, approval_id="approval-1", expires_at=expires_at),
    )


def _operation_request(
    operation: TrustOperation,
    grant: AuthorityGrant,
    *,
    principal_type: PrincipalType = PrincipalType.OPERATOR,
) -> TrustRequest:
    request = _request()
    return replace(
        request,
        principal=replace(
            request.principal,
            principal_type=principal_type,
            grants=(grant,),
        ),
        operation=operation,
        required_grant=grant,
    )


def test_local_only_data_allows_local_and_trusted_lan_destinations():
    local = evaluate_trust(_request())
    lan = evaluate_trust(
        _request(destination_class=DestinationClass.TRUSTED_LAN_RUNTIME)
    )

    assert local.allowed is True
    assert lan.allowed is True
    assert local.reason_code == "policy_allowed"


def test_local_only_data_denies_remote_destination():
    decision = evaluate_trust(
        _request(destination_class=DestinationClass.REMOTE_PROVIDER)
    )

    assert decision.allowed is False
    assert decision.reason_code == "local_only_egress_blocked"


def test_cloud_egress_requires_redaction_when_declared():
    unsafe = evaluate_trust(
        _request(
            egress_class=EgressClass.CLOUD_ALLOWED_REDACTED,
            destination_class=DestinationClass.REMOTE_PROVIDER,
        )
    )
    safe = evaluate_trust(
        _request(
            egress_class=EgressClass.CLOUD_ALLOWED_REDACTED,
            destination_class=DestinationClass.REMOTE_PROVIDER,
            redaction_applied=True,
        )
    )

    assert unsafe.reason_code == "cloud_redaction_required"
    assert safe.allowed is True


def test_cloud_allowed_full_can_reach_declared_remote_destination():
    decision = evaluate_trust(
        _request(
            egress_class=EgressClass.CLOUD_ALLOWED_FULL,
            destination_class=DestinationClass.REMOTE_PROVIDER,
        )
    )

    assert decision.allowed is True


def test_provenance_join_is_monotonic_and_cannot_be_widened():
    request = _request(
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        provenance_egress=EgressClass.LOCAL_ONLY,
        destination_class=DestinationClass.REMOTE_PROVIDER,
    )

    assert effective_egress_class(request.provenance) is EgressClass.LOCAL_ONLY
    decision = evaluate_trust(request)
    assert decision.allowed is False
    assert decision.reason_code == "egress_class_widening"


def test_mixed_provenance_uses_most_restrictive_egress_class():
    request = _request(egress_class=EgressClass.LOCAL_ONLY)
    request = replace(
        request,
        provenance=(
            request.provenance[0],
            TrustProvenance(
                origin=ContentOrigin.SERAPH_CONTROL,
                source_id="system-policy",
                data_digest=_digest("policy"),
                egress_class=EgressClass.CLOUD_ALLOWED_FULL,
                instruction_authority=True,
            ),
        ),
    )

    assert effective_egress_class(request.provenance) is EgressClass.LOCAL_ONLY
    assert evaluate_trust(request).allowed is True


def test_untrusted_provenance_cannot_claim_instruction_authority():
    decision = evaluate_trust(
        _request(
            origin=ContentOrigin.EXTERNAL_UNTRUSTED,
            instruction_authority=True,
        )
    )

    assert decision.allowed is False
    assert decision.reason_code == "untrusted_instruction_authority"


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda request: replace(request, policy_version="unknown"), "unknown_policy_version"),
        (
            lambda request: replace(
                request,
                principal=TrustPrincipal(principal_id="", principal_type=""),
            ),
            "principal_missing",
        ),
        (lambda request: replace(request, operation="unknown"), "operation_unknown"),
        (lambda request: replace(request, capability_id=""), "capability_missing"),
        (lambda request: replace(request, capability_version=""), "capability_version_missing"),
        (lambda request: replace(request, data_digest=""), "data_digest_invalid"),
        (lambda request: replace(request, secret_scope_digest=""), "secret_scope_digest_invalid"),
        (lambda request: replace(request, resource_limits_digest=""), "resource_limits_digest_invalid"),
        (
            lambda request: replace(
                request,
                destination=TrustDestination(destination_id="", destination_class="unknown"),
            ),
            "destination_unknown",
        ),
        (lambda request: replace(request, egress_class="unknown"), "egress_class_unknown"),
        (lambda request: replace(request, provenance=()), "provenance_missing"),
    ],
)
def test_missing_or_unknown_required_contract_fields_deny(mutator, reason):
    decision = evaluate_trust(mutator(_request()))

    assert decision.allowed is False
    assert decision.reason_code == reason


def test_required_audit_and_recovery_bindings_fail_closed():
    audit_missing = replace(_request(), audit=AuditBinding(required=True))
    recovery_missing = replace(_request(), recovery=RecoveryBinding(required=True))

    assert evaluate_trust(audit_missing).reason_code == "audit_binding_missing"
    assert evaluate_trust(recovery_missing).reason_code == "recovery_binding_missing"


def test_exact_approval_binding_allows_request_before_expiry():
    request = _approved(_request(approval_required=True))

    decision = evaluate_trust(request, now=100.0)
    assert decision.allowed is True


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda request: replace(
                request,
                approval=replace(request.approval, expires_at=99.0),
            ),
            "approval_expired",
        ),
        (
            lambda request: replace(
                request,
                approval=replace(request.approval, policy_version="old-policy"),
            ),
            "approval_policy_drift",
        ),
        (
            lambda request: replace(
                request,
                destination=replace(request.destination, destination_id="runtime-2"),
            ),
            "approval_destination_drift",
        ),
        (
            lambda request: replace(request, capability_id="report_agent"),
            "approval_capability_drift",
        ),
        (
            lambda request: replace(request, capability_version="v2"),
            "approval_capability_version_drift",
        ),
        (
            lambda request: replace(request, data_digest=_digest("different-data")),
            "approval_data_drift",
        ),
        (
            lambda request: replace(request, resource_limits_digest=_digest("limits-v2")),
            "approval_resource_limits_drift",
        ),
    ],
)
def test_approval_binding_rejects_expiry_and_scope_drift(mutator, reason):
    request = _approved(_request(approval_required=True))
    drifted = mutator(request)

    decision = evaluate_trust(drifted, now=100.0)
    assert decision.allowed is False
    assert decision.reason_code == reason


def test_approval_binding_rejects_secret_scope_drift_for_capability_call():
    request = replace(
        _request(approval_required=True),
        principal=replace(
            _request().principal,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        ),
        operation=TrustOperation.CAPABILITY_CALL,
        required_grant=AuthorityGrant.CAPABILITY_EXECUTE,
        secret_scope_digest=_digest("secret-scope-v1"),
    )
    request = _approved(request)

    drifted = replace(request, secret_scope_digest=_digest("secret-scope-v2"))
    decision = evaluate_trust(drifted, now=100.0)
    assert decision.allowed is False
    assert decision.reason_code == "approval_secret_scope_drift"


def test_approval_binding_rejects_consumed_and_replayed_approval():
    request = _approved(_request(approval_required=True))
    consumed = replace(request, approval=replace(request.approval, consumed=True))

    assert evaluate_trust(consumed, now=100.0).reason_code == "approval_replayed"
    assert evaluate_trust(
        request,
        now=100.0,
        replayed_approval_ids={"approval-1"},
    ).reason_code == "approval_replayed"


def test_decision_and_digest_are_deterministic_and_do_not_contain_payload_or_secret():
    secret = "super-secret-payload-value"
    request = _request()
    request = replace(
        request,
        provenance=(replace(request.provenance[0], source_id=f"ref:{secret}"),),
    )

    first = evaluate_trust(request)
    second = evaluate_trust(request)
    serialized = json.dumps(first.as_dict(), sort_keys=True)

    assert first == second
    assert first.schema_version == TRUST_SCHEMA_VERSION
    assert secret not in first.request_digest
    assert secret not in first.decision_id
    assert secret not in serialized
    assert "content" not in first.as_dict()
    assert "secret" not in first.as_dict()


def test_approval_request_digest_detects_non_binding_request_drift():
    request = _approved(_request(approval_required=True))
    drifted = replace(request, redaction_applied=True)

    decision = evaluate_trust(drifted, now=100.0)
    assert decision.allowed is False
    assert decision.reason_code == "approval_request_drift"


def test_external_mutation_contract_can_require_exact_approval_audit_and_recovery():
    request = replace(
        _request(
            egress_class=EgressClass.CLOUD_ALLOWED_FULL,
            destination_class=DestinationClass.EXTERNAL_SYSTEM,
            approval_required=True,
        ),
        principal=replace(
            _request().principal,
            grants=(AuthorityGrant.EXTERNAL_MUTATION,),
        ),
        operation=TrustOperation.EXTERNAL_MUTATION,
        required_grant=AuthorityGrant.EXTERNAL_MUTATION,
        audit=AuditBinding(receipt_id="audit-1", required=True, persisted=True, durable=True),
        recovery=RecoveryBinding(recovery_class=RecoveryClass.COMPENSATE, required=True),
    )
    request = replace(
        request,
        authority_scope_digest=authority_scope_digest(
            required_grant=request.required_grant,
            capability_id=request.capability_id,
            destination=request.destination,
            resource=request.resource,
        ),
    )
    request = _approved(request)

    assert evaluate_trust(
        request,
        now=100.0,
        verified_audit_receipt_ids={"audit-1"},
    ).allowed is True


def test_non_discretionary_external_mutation_requires_approval_audit_and_recovery():
    base = replace(
        _request(
            egress_class=EgressClass.CLOUD_ALLOWED_FULL,
            destination_class=DestinationClass.EXTERNAL_SYSTEM,
        ),
        principal=replace(
            _request().principal,
            grants=(AuthorityGrant.EXTERNAL_MUTATION,),
        ),
        operation=TrustOperation.EXTERNAL_MUTATION,
        required_grant=AuthorityGrant.EXTERNAL_MUTATION,
    )

    assert evaluate_trust(base, now=100.0).reason_code == "audit_binding_missing"
    with_audit = replace(
        base,
        audit=AuditBinding(receipt_id="audit-1", persisted=True, durable=True),
    )
    assert evaluate_trust(
        with_audit,
        now=100.0,
        verified_audit_receipt_ids={"audit-1"},
    ).reason_code == "recovery_binding_missing"
    ready_for_approval = replace(
        with_audit,
        recovery=RecoveryBinding(recovery_class=RecoveryClass.COMPENSATE),
    )
    decision = evaluate_trust(
        ready_for_approval,
        now=100.0,
        verified_audit_receipt_ids={"audit-1"},
    )
    assert decision.effect.value == "require_approval"
    assert decision.reason_code == "approval_missing"


def test_credential_egress_also_has_non_discretionary_bindings():
    request = replace(
        _request(
            egress_class=EgressClass.CLOUD_ALLOWED_FULL,
            destination_class=DestinationClass.MANAGED_CONNECTOR,
        ),
        principal=replace(
            _request().principal,
            grants=(AuthorityGrant.CREDENTIAL_EGRESS,),
        ),
        operation=TrustOperation.CREDENTIAL_EGRESS,
        required_grant=AuthorityGrant.CREDENTIAL_EGRESS,
        secret_scope_digest=_digest("scoped-secret-ref-v1"),
        audit=AuditBinding(receipt_id="audit-1", persisted=True, durable=True),
        recovery=RecoveryBinding(recovery_class=RecoveryClass.QUARANTINE),
    )

    decision = evaluate_trust(
        request,
        now=100.0,
        verified_audit_receipt_ids={"audit-1"},
    )
    assert decision.effect.value == "require_approval"
    assert decision.reason_code == "approval_missing"


def test_model_inference_denies_any_secret_scope():
    decision = evaluate_trust(replace(_request(), secret_scope_digest=_digest("secret-scope-v1")))

    assert decision.allowed is False
    assert decision.reason_code == "secret_scope_forbidden_for_model"


def test_revoked_principal_is_denied():
    request = replace(
        _request(),
        principal=replace(_request().principal, revoked=True),
    )

    assert evaluate_trust(request).reason_code == "principal_revoked"


@pytest.mark.parametrize(
    "principal",
    [
        TrustPrincipal(
            principal_id="anonymous",
            principal_type=PrincipalType.ANONYMOUS,
            authenticated=False,
        ),
        TrustPrincipal(
            principal_id="forged-edge",
            principal_type=PrincipalType.PAIRED_EDGE,
            authenticated=False,
        ),
        TrustPrincipal(
            principal_id="unknown",
            principal_type="unknown",
            authenticated=True,
        ),
    ],
)
def test_ingress_denies_anonymous_forged_and_unknown_principals(principal):
    request = replace(
        _request(),
        principal=principal,
        operation=TrustOperation.INGRESS,
        capability_id="paired_edge_ingress",
    )

    decision = evaluate_trust(request)
    assert decision.allowed is False
    assert decision.reason_code == "principal_unauthorized"


def test_model_inference_denies_unauthenticated_principal():
    request = replace(
        _request(),
        principal=TrustPrincipal(
            principal_id="unpaired-client",
            principal_type=PrincipalType.OPERATOR,
            authenticated=False,
        ),
    )

    decision = evaluate_trust(request)
    assert decision.allowed is False
    assert decision.reason_code == "principal_unauthorized"


def test_unknown_provenance_egress_class_denies():
    decision = evaluate_trust(
        _request(egress_class=EgressClass.LOCAL_ONLY, provenance_egress="unknown")
    )

    assert decision.allowed is False
    assert decision.reason_code == "provenance_egress_unknown"


def test_authentication_does_not_grant_paired_edge_model_authority():
    request = _operation_request(
        TrustOperation.MODEL_INFERENCE,
        AuthorityGrant.MODEL_INFERENCE,
        principal_type=PrincipalType.PAIRED_EDGE,
    )

    assert evaluate_trust(request).reason_code == "principal_operation_forbidden"


def test_authenticated_but_under_scoped_operator_is_denied():
    request = replace(_request(), principal=replace(_request().principal, grants=()))

    assert evaluate_trust(request).reason_code == "authority_grant_missing"


@pytest.mark.parametrize(
    ("principal", "operation", "required_grant", "reason"),
    [
        (
            TrustPrincipal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
                authenticated=False,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
                session_id="session-1",
            ),
            TrustOperation.MODEL_INFERENCE,
            AuthorityGrant.MODEL_INFERENCE,
            "principal_unauthorized",
        ),
        (
            TrustPrincipal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
                revoked=True,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
                session_id="session-1",
            ),
            TrustOperation.MODEL_INFERENCE,
            AuthorityGrant.MODEL_INFERENCE,
            "principal_revoked",
        ),
        (
            TrustPrincipal(
                principal_id="edge-1",
                principal_type=PrincipalType.PAIRED_EDGE,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
                session_id="session-1",
            ),
            TrustOperation.MODEL_INFERENCE,
            AuthorityGrant.MODEL_INFERENCE,
            "principal_operation_forbidden",
        ),
        (
            TrustPrincipal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
                session_id="session-1",
            ),
            TrustOperation.MODEL_INFERENCE,
            AuthorityGrant.CAPABILITY_EXECUTE,
            "required_grant_invalid",
        ),
        (
            TrustPrincipal(
                principal_id="operator-1",
                principal_type=PrincipalType.OPERATOR,
                grants=(),
                session_id="session-1",
            ),
            TrustOperation.MODEL_INFERENCE,
            AuthorityGrant.MODEL_INFERENCE,
            "authority_grant_missing",
        ),
    ],
)
def test_principal_operation_helper_matches_full_evaluator_early_reasons(
    principal,
    operation,
    required_grant,
    reason,
):
    request = replace(
        _request(),
        principal=principal,
        operation=operation,
        required_grant=required_grant,
    )

    assert principal_operation_reason(
        principal=principal,
        operation=operation,
        required_grant=required_grant,
    ) == reason
    assert evaluate_trust(request).reason_code == reason


def test_principal_operation_helper_allows_exact_operator_grant():
    request = _request()

    assert principal_operation_reason(
        principal=request.principal,
        operation=request.operation,
        required_grant=request.required_grant,
    ) is None


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda request: replace(request, session_id="session-2"),
            "principal_session_mismatch",
        ),
        (
            lambda request: replace(
                request,
                principal=replace(request.principal, job_id="job-1"),
            ),
            "principal_job_mismatch",
        ),
    ],
)
def test_principal_must_match_exact_session_and_job(mutator, reason):
    assert evaluate_trust(mutator(_request())).reason_code == reason


def test_request_attempt_replay_and_expiry_are_fail_closed():
    request = _request()

    assert evaluate_trust(
        request,
        replayed_attempt_ids={request.attempt_id},
    ).reason_code == "request_replayed"
    assert evaluate_trust(
        request,
        replayed_replay_ids={request.replay_id},
    ).reason_code == "request_replayed"
    assert evaluate_trust(replace(request, decision_expires_at=100.0)).reason_code == "decision_expired"
    assert evaluate_trust(replace(request, decision_expires_at=401.0)).reason_code == "decision_expiry_unbounded"


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda request: replace(request, data_digest="A" * 64), "data_digest_invalid"),
        (
            lambda request: replace(request, transformation_digest="not-a-digest"),
            "transformation_digest_invalid",
        ),
        (
            lambda request: replace(
                request,
                provenance=(replace(request.provenance[0], data_digest="f" * 63),),
            ),
            "provenance_digest_invalid",
        ),
    ],
)
def test_all_persistable_digest_metadata_requires_canonical_sha256(mutator, reason):
    assert evaluate_trust(mutator(_request())).reason_code == reason


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda request: replace(request, transformation_digest=_digest("transformed")),
            "approval_transformation_drift",
        ),
        (
            lambda request: replace(request, authority_scope_digest=_digest("new-authority")),
            "approval_authority_scope_drift",
        ),
        (
            lambda request: replace(
                request,
                resource=replace(request.resource, resource_id="runtime-2"),
            ),
            "approval_resource_drift",
        ),
        (
            lambda request: replace(
                request,
                principal=replace(request.principal, session_id="session-2"),
                session_id="session-2",
            ),
            "approval_session_drift",
        ),
        (lambda request: replace(request, request_id="request-2"), "approval_request_id_drift"),
        (lambda request: replace(request, attempt_id="attempt-2"), "approval_attempt_drift"),
        (lambda request: replace(request, replay_id="replay-2"), "approval_replay_drift"),
        (
            lambda request: replace(request, decision_expires_at=140.0),
            "approval_decision_expiry_drift",
        ),
    ],
)
def test_approval_binds_new_authority_and_attempt_scope(mutator, reason):
    request = _approved(_request(approval_required=True))

    assert evaluate_trust(mutator(request)).reason_code == reason


@pytest.mark.parametrize(
    "audit",
    [
        AuditBinding(receipt_id="audit-1", required=True, persisted=False, durable=True),
        AuditBinding(receipt_id="audit-1", required=True, persisted=True, durable=False),
    ],
)
def test_required_audit_must_be_persisted_and_durable(audit):
    assert evaluate_trust(replace(_request(), audit=audit)).reason_code == "audit_binding_missing"


def test_caller_asserted_durable_audit_requires_authoritative_verification():
    request = replace(
        _request(),
        audit=AuditBinding(receipt_id="audit-1", required=True, persisted=True, durable=True),
    )

    assert evaluate_trust(request).reason_code == "audit_receipt_unverified"
    assert evaluate_trust(
        request,
        verified_audit_receipt_ids={"audit-1"},
    ).allowed is True
    assert evaluate_trust(
        request,
        verified_audit_receipt_ids=(),
    ).reason_code == "audit_receipt_unverified"


def test_recovery_class_rejects_unknown_value():
    request = replace(_request(), recovery=RecoveryBinding(recovery_class="rollback", required=True))

    assert evaluate_trust(request).reason_code == "recovery_class_invalid"


@pytest.mark.parametrize("recovery_class", [RecoveryClass.QUARANTINE, RecoveryClass.IRREVERSIBLE])
def test_required_quarantine_and_irreversible_recovery_are_explicit(recovery_class):
    request = replace(
        _request(),
        recovery=RecoveryBinding(recovery_class=recovery_class, required=True),
    )

    assert evaluate_trust(request).allowed is True
