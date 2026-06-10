"""Batch CT container-grade secure-host validation receipts.

This module records stronger secure-host implementation evidence, external
validation, and secret-egress certification drills above BW/CD/CK. It names
container/Wasm/VM/TEE decisions honestly and does not claim hardware-backed
isolation, production security solved, IronClaw-class execution, production
readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


CONTAINER_GRADE_CAPABILITY_ISOLATION_SUITE_NAME = "container_grade_capability_isolation"
CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES = (
    "container_grade_isolation_model_behavior",
    "container_grade_capability_class_enforcement_behavior",
    "container_grade_signed_tool_root_behavior",
    "container_grade_missing_hardware_boundary_behavior",
    "container_grade_operator_surface_behavior",
)
EXTERNAL_SECURITY_VALIDATION_V1_SUITE_NAME = "external_security_validation_v1"
EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES = (
    "external_security_review_scope_behavior",
    "external_security_findings_remediation_behavior",
    "external_security_waiver_residual_risk_behavior",
    "external_security_incident_recovery_behavior",
)
SECRET_EGRESS_CERTIFICATION_DRILL_SUITE_NAME = "secret_egress_certification_drill"
SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES = (
    "secret_egress_destination_allowlist_behavior",
    "secret_egress_raw_value_denial_behavior",
    "secret_egress_rotation_recovery_behavior",
    "secret_egress_operator_receipt_behavior",
)

CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY = (
    "container_grade_secure_host_validation_receipts_not_hardware_backed_or_certified_isolation"
)
CONTAINER_GRADE_SECURE_HOST_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "hardware_backed_isolation",
    "container_grade_isolation_implemented",
    "cvm_tee_wasm_or_container_isolation_implemented",
    "tee_cvm_wasm_or_container_runtime_isolation",
    "full_host_container_isolation",
    "external_security_certification",
    "certified_isolation",
    "production_penetration_test_passed",
    "safe_autonomous_computer_use",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def container_grade_secure_host_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            CONTAINER_GRADE_CAPABILITY_ISOLATION_SUITE_NAME,
            EXTERNAL_SECURITY_VALIDATION_V1_SUITE_NAME,
            SECRET_EGRESS_CERTIFICATION_DRILL_SUITE_NAME,
        ],
        "foundation_suites": [
            "secure_capability_host",
            "production_secure_host_hardening",
            "production_isolation_hardening_v2",
            "independent_secure_host_review",
        ],
        "claim_boundary": CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        "blocked_claims": list(CONTAINER_GRADE_SECURE_HOST_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/container-grade-secure-host",
            "/api/operator/independent-secure-host-review",
            "/api/operator/production-isolation-hardening",
            "/api/operator/secure-capability-host-hardening",
            "/api/operator/benchmark-proof",
            "GitHub issue #524",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "evidence_policy": (
            "container-grade secure-host validation must separate enforced local boundaries, external review "
            "findings, secret-egress certification drills, and unsupported hardware-backed/runtime-isolation "
            "claims before stronger security wording is allowed"
        ),
        "not_claimed": [
            "secure_private_by_default",
            "production_security_solved",
            "ironclaw_class_secure_execution",
            "hardware_backed_isolation",
            "tee_cvm_wasm_or_container_runtime_isolation",
            "production_ready_product",
            "full_parity_achieved",
        ],
    }


def isolation_model_decision_records() -> list[dict[str, Any]]:
    return [
        _isolation_decision(
            "ct-tool-process",
            "tool_process",
            "disposable_worker_root_with_signed_tool_manifest",
            ["workspace_write_scope", "allowlisted_environment", "nonzero_timeout_receipts"],
            "container_or_vm_not_implemented_for_general_tool_processes",
            implemented=True,
        ),
        _isolation_decision(
            "ct-browser-automation",
            "browser_automation",
            "fresh_profile_context_with_download_upload_boundaries",
            ["per_run_profile", "cookie_storage_not_exported", "download_redaction_receipt"],
            "full_browser_credential_isolation_not_certified",
            implemented=True,
        ),
        _isolation_decision(
            "ct-authenticated-connector",
            "authenticated_connector",
            "credential_broker_secret_ref_proxy",
            ["destination_host_allowlist", "field_scoped_secret_ref", "raw_secret_denial"],
            "external_vault_or_hardware_key_attestation_not_implemented",
            implemented=True,
        ),
        _isolation_decision(
            "ct-extension-runtime",
            "extension_runtime",
            "signed_package_root_and_quarantine_before_runtime_contribution",
            ["publisher_key_digest", "sbom_digest", "permission_delta_review"],
            "third_party_package_security_not_solved",
            implemented=True,
        ),
        _isolation_decision(
            "ct-workflow-replay",
            "workflow_replay",
            "same_or_narrower_trust_checkpoint_resume",
            ["approval_context_digest", "credential_scope_digest", "operator_recovery_authority"],
            "distributed_secure_workflow_engine_not_implemented",
            implemented=True,
        ),
        _isolation_decision(
            "ct-hardware-backed-runtime",
            "hardware_backed_runtime",
            "unsupported_boundary_declared",
            ["not_enabled", "claim_blocked", "operator_visible_boundary"],
            "tee_cvm_wasm_container_runtime_isolation_not_implemented",
            implemented=False,
        ),
    ]


def external_security_validation_receipts() -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": "ct-external-review-scope-2026-06",
            "reviewer": "external_security_validation_fixture",
            "review_date": "2026-06-10",
            "evidence_mode": "external_review_record",
            "scope": [
                "tool_process_isolation",
                "browser_profile_boundary",
                "connector_secret_broker",
                "extension_package_root",
                "network_egress_policy",
                "incident_recovery",
            ],
            "artifacts": ["review_scope", "finding_register", "remediation_receipts", "residual_risk_waivers"],
            "artifact_digest": "sha256:ct-review-scope-2026-06-fixture",
            "declared_fixture_path": "security-reviews/ct/external-validation-scope-2026-06.fixture.json",
            "reviewer_independence": "fixture_external_to_runtime_implementation",
            "certification_status": "not_certified_fixture_record",
            "real_external_certification": False,
            "evidence_boundary": "fixture_validation_record_not_external_security_certification",
            "residual_risks": [
                "browser_download_boundary_requires_live_provider_certification",
                "hardware_backed_runtime_isolation_not_implemented",
            ],
            "operator_visible": True,
        },
        {
            "receipt_id": "ct-external-findings-register",
            "reviewer": "external_security_validation_fixture",
            "review_date": "2026-06-10",
            "evidence_mode": "finding_remediation_record",
            "artifact_digest": "sha256:ct-finding-register-2026-06-fixture",
            "declared_fixture_path": "security-reviews/ct/finding-register-2026-06.fixture.json",
            "certification_status": "not_certified_fixture_record",
            "real_external_certification": False,
            "evidence_boundary": "fixture_validation_record_not_external_security_certification",
            "findings": [
                _finding("CT-SEC-001", "high", "raw_secret_echo_attempt", "remediated"),
                _finding("CT-SEC-002", "high", "private_network_redirect", "remediated"),
                _finding("CT-SEC-003", "medium", "unsigned_tool_root", "remediated"),
                _finding("CT-SEC-004", "medium", "browser_download_boundary", "residual_risk_waiver"),
            ],
            "operator_visible": True,
        },
    ]


def secret_egress_certification_drills() -> list[dict[str, Any]]:
    return [
        _secret_egress_drill(
            "ct-egress-allowed-connector",
            "authenticated_connector",
            "api.partner.example",
            "allowed",
            "secret_ref_forwarded_to_allowlisted_host_only",
            leaked=False,
        ),
        _secret_egress_drill(
            "ct-egress-private-network",
            "browser_fetch",
            "169.254.169.254",
            "blocked",
            "private_network_destination_denied",
            leaked=False,
        ),
        _secret_egress_drill(
            "ct-egress-raw-secret-output",
            "tool_process",
            "operator_transcript",
            "blocked",
            "raw_secret_value_redacted_and_rotation_required",
            leaked=False,
            rotation_required=True,
        ),
        _secret_egress_drill(
            "ct-egress-destination-drift",
            "external_mcp",
            "unexpected.example",
            "blocked",
            "destination_host_mismatch_requires_new_approval",
            leaked=False,
        ),
        _secret_egress_drill(
            "ct-egress-private-redirect",
            "authenticated_connector",
            "api.partner.example -> 10.0.0.5",
            "blocked",
            "redirect_to_private_network_destination_denied",
            leaked=False,
        ),
        _secret_egress_drill(
            "ct-egress-expired-secret-ref",
            "workflow_replay",
            "api.partner.example",
            "blocked",
            "expired_or_cross_session_secret_ref_requires_rotation",
            leaked=False,
            rotation_required=True,
        ),
    ]


def incident_recovery_validation_receipts() -> list[dict[str, Any]]:
    return [
        _incident_recovery("rotate", "secret_egress_boundary_drift", "credential_rotation_receipt", True),
        _incident_recovery("quarantine", "unsigned_or_downgraded_tool_root", "capability_quarantine_receipt", True),
        _incident_recovery("kill_switch", "privileged_boundary_regression", "privileged_family_disabled", True),
        _incident_recovery("waive", "documented_browser_download_residual", "time_bounded_residual_risk_waiver", False),
        _incident_recovery("post_incident_audit", "security_boundary_event", "operator_visible_audit_bundle", False),
    ]


def build_container_grade_secure_host_contract() -> dict[str, Any]:
    isolation = isolation_model_decision_records()
    validation = external_security_validation_receipts()
    egress = secret_egress_certification_drills()
    recovery = incident_recovery_validation_receipts()
    findings = [
        finding
        for receipt in validation
        for finding in receipt.get("findings", [])
        if isinstance(finding, dict)
    ]
    receipt_index = {
        "isolation_model_decisions": isolation,
        "external_security_validation": validation,
        "secret_egress_certification": egress,
        "incident_recovery_validation": recovery,
    }
    return {
        "summary": {
            "suite_name": "container_grade_secure_host",
            "operator_status": "container_grade_secure_host_validation_visible",
            "isolation_decision_count": len(isolation),
            "implemented_boundary_count": sum(1 for item in isolation if item.get("implemented")),
            "unsupported_boundary_count": sum(1 for item in isolation if not item.get("implemented")),
            "external_review_count": len(validation),
            "finding_count": len(findings),
            "remediated_or_waived_findings": sum(
                1 for item in findings if item.get("status") in {"remediated", "residual_risk_waiver"}
            ),
            "secret_egress_drill_count": len(egress),
            "secret_leak_count": sum(1 for item in egress if item.get("raw_secret_leaked")),
            "recovery_authority_count": len(recovery),
            "all_secret_drills_safe": all(not item.get("raw_secret_leaked") for item in egress),
            "missing_hardware_boundary_visible": any(
                item["capability_class"] == "hardware_backed_runtime" and not item["implemented"] for item in isolation
            ),
            "receipt_digest": _stable_digest(receipt_index),
            "claim_boundary": CONTAINER_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        },
        "isolation_model_decisions": isolation,
        "external_security_validation_receipts": validation,
        "secret_egress_certification_drills": egress,
        "incident_recovery_validation_receipts": recovery,
        "policy": container_grade_secure_host_policy_payload(),
    }


def _isolation_decision(
    receipt_id: str,
    capability_class: str,
    enforcement_model: str,
    enforced_boundaries: list[str],
    residual_boundary: str,
    *,
    implemented: bool,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "capability_class": capability_class,
        "enforcement_model": enforcement_model,
        "implemented": implemented,
        "enforced_boundaries": enforced_boundaries,
        "signed_root_required": capability_class in {"tool_process", "extension_runtime"},
        "network_policy_required": capability_class in {"authenticated_connector", "browser_automation"},
        "credential_broker_required": capability_class in {"authenticated_connector", "workflow_replay"},
        "operator_visible": True,
        "residual_boundary": residual_boundary,
    }


def _finding(finding_id: str, severity: str, title: str, status: str) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "title": title,
        "status": status,
        "waiver_expires": "2026-07-10" if status == "residual_risk_waiver" else None,
        "operator_notification": True,
        "receipt": f"security-validation:{finding_id}:{status}",
    }


def _secret_egress_drill(
    drill_id: str,
    surface: str,
    destination_host: str,
    decision: str,
    reason: str,
    *,
    leaked: bool,
    rotation_required: bool = False,
) -> dict[str, Any]:
    return {
        "drill_id": drill_id,
        "surface": surface,
        "destination_host": destination_host,
        "decision": decision,
        "reason": reason,
        "raw_secret_leaked": leaked,
        "rotation_required": rotation_required,
        "operator_visible": True,
        "receipt_after_drill": f"secret-egress-certification:{drill_id}:{decision}",
    }


def _incident_recovery(action: str, trigger: str, receipt: str, requires_review: bool) -> dict[str, Any]:
    return {
        "action": action,
        "trigger": trigger,
        "receipt": receipt,
        "enabled": True,
        "requires_review": requires_review,
        "operator_visible": True,
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Container-grade secure-host scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_container_grade_secure_host_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        CONTAINER_GRADE_CAPABILITY_ISOLATION_SUITE_NAME,
        EXTERNAL_SECURITY_VALIDATION_V1_SUITE_NAME,
        SECRET_EGRESS_CERTIFICATION_DRILL_SUITE_NAME,
    ])


async def build_container_grade_secure_host_report() -> dict[str, Any]:
    summary = await _run_container_grade_secure_host_suites()
    contract = build_container_grade_secure_host_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "container_grade_secure_host_ci_gated_operator_visible"
                if healthy
                else "container_grade_secure_host_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES)
                + len(EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES)
                + len(SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            CONTAINER_GRADE_CAPABILITY_ISOLATION_SUITE_NAME: list(
                CONTAINER_GRADE_CAPABILITY_ISOLATION_SCENARIO_NAMES
            ),
            EXTERNAL_SECURITY_VALIDATION_V1_SUITE_NAME: list(EXTERNAL_SECURITY_VALIDATION_V1_SCENARIO_NAMES),
            SECRET_EGRESS_CERTIFICATION_DRILL_SUITE_NAME: list(SECRET_EGRESS_CERTIFICATION_DRILL_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name=CONTAINER_GRADE_CAPABILITY_ISOLATION_SUITE_NAME),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
