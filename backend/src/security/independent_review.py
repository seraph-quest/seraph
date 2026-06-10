"""Batch CK independent secure-host review and isolation proof receipts.

This module records the stronger proof layer above Batch BW and Batch CD. The
receipts are intentionally bounded: they make independent review, hostile
drills, isolation evidence, and recovery controls visible without claiming
secure/private-by-default execution, IronClaw-class security, production
security solved, full host/container/TEE/CVM/Wasm isolation, production
readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

from typing import Any


INDEPENDENT_SECURE_HOST_REVIEW_SUITE_NAME = "independent_secure_host_review"
INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES = (
    "independent_security_review_scope_behavior",
    "independent_security_findings_remediation_behavior",
    "independent_isolation_evidence_matrix_behavior",
    "operator_independent_security_review_surface_behavior",
)

LIVE_HOSTILE_ISOLATION_DRILLS_SUITE_NAME = "live_hostile_isolation_drills"
LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES = (
    "live_hostile_prompt_injection_drill_behavior",
    "live_hostile_ssrf_private_egress_drill_behavior",
    "live_hostile_filesystem_escape_drill_behavior",
    "live_hostile_credential_exfiltration_drill_behavior",
    "live_hostile_extension_permission_creep_drill_behavior",
    "live_hostile_replay_approval_drift_drill_behavior",
    "live_hostile_browser_session_bleed_drill_behavior",
)

SECURE_HOST_RECOVERY_AUTHORITY_SUITE_NAME = "secure_host_recovery_authority"
SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES = (
    "secure_host_operator_allow_deny_quarantine_behavior",
    "secure_host_credential_rotation_recovery_behavior",
    "secure_host_post_incident_audit_behavior",
    "secure_host_claim_boundary_after_review_behavior",
)

INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY = (
    "independent_secure_host_review_receipts_not_secure_private_or_ironclaw_class"
)

INDEPENDENT_SECURE_HOST_REVIEW_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "full_host_container_isolation",
    "tee_cvm_or_wasm_isolation_implemented",
    "safe_autonomous_computer_use",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)


def independent_secure_host_review_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            INDEPENDENT_SECURE_HOST_REVIEW_SUITE_NAME,
            LIVE_HOSTILE_ISOLATION_DRILLS_SUITE_NAME,
            SECURE_HOST_RECOVERY_AUTHORITY_SUITE_NAME,
        ],
        "foundation_suites": [
            "production_secure_host_hardening",
            "secure_capability_host_live_isolation_v2",
            "production_isolation_hardening_v2",
            "privileged_path_red_team_gauntlet_v2",
            "security_incident_recovery_drill",
        ],
        "claim_boundary": INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
        "blocked_claims": list(INDEPENDENT_SECURE_HOST_REVIEW_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/independent-secure-host-review",
            "/api/operator/production-isolation-hardening",
            "/api/operator/secure-capability-host-hardening",
            "/api/operator/benchmark-proof",
            "/api/operator/final-parity-readiness-report",
        ],
        "evidence_policy": (
            "independent review and hostile drill receipts must separate reviewer-attested "
            "findings from deterministic contracts and must label unsupported isolation claims"
        ),
        "not_claimed": [
            "secure_private_by_default",
            "production_security_solved",
            "ironclaw_class_secure_execution",
            "full_host_container_isolation",
            "tee_cvm_wasm_or_container_runtime_isolation",
            "production_ready_product",
            "full_parity_achieved",
        ],
    }


def independent_security_review_receipts() -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": "ck-review-scope-2026-06",
            "reviewer": "independent_security_reviewer_fixture",
            "review_date": "2026-06-10",
            "evidence_mode": "independent_review_record",
            "target_surfaces": [
                "tool_runtime",
                "browser_computer_use",
                "connector_credentials",
                "workflow_replay",
                "extension_runtime_contributions",
                "background_execution",
            ],
            "review_artifacts": [
                "architecture_boundary_matrix",
                "hostile_drill_transcript",
                "finding_remediation_register",
            ],
            "operator_visible": True,
            "residual_risk": INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
        },
        {
            "receipt_id": "ck-review-findings-register",
            "reviewer": "independent_security_reviewer_fixture",
            "review_date": "2026-06-10",
            "evidence_mode": "finding_remediation_record",
            "findings": [
                _review_finding("CK-SEC-001", "high", "prompt_injection_delegation", "remediated"),
                _review_finding("CK-SEC-002", "high", "credential_exfiltration_replay", "remediated"),
                _review_finding("CK-SEC-003", "medium", "extension_permission_creep", "remediated"),
                _review_finding("CK-SEC-004", "medium", "browser_profile_reuse", "residual_risk_documented"),
            ],
            "operator_visible": True,
            "residual_risk": INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
        },
    ]


def isolation_evidence_matrix() -> list[dict[str, Any]]:
    return [
        _isolation_evidence(
            "tool_worker_root",
            "disposable_worker_root_outside_workspace",
            "deterministic_contract_plus_review",
            "not_container_or_vm_escape_proof",
        ),
        _isolation_evidence(
            "browser_profile",
            "per_session_profile_and_storage_state_omission",
            "recorded_live_drill_plus_review",
            "not_full_browser_credential_isolation",
        ),
        _isolation_evidence(
            "connector_credential",
            "field_scoped_secret_ref_destination_allowlist",
            "deterministic_contract_plus_review",
            "not_external_vault_or_hardware_attestation",
        ),
        _isolation_evidence(
            "extension_runtime",
            "revocation_quarantine_before_runtime_contribution",
            "recorded_live_drill_plus_review",
            "not_third_party_package_security_solution",
        ),
        _isolation_evidence(
            "workflow_replay",
            "same_or_narrower_trust_replay_authority",
            "deterministic_contract_plus_review",
            "not_distributed_workflow_security_certification",
        ),
        _isolation_evidence(
            "hardware_backed_isolation",
            "unsupported",
            "explicitly_not_claimed",
            "tee_cvm_wasm_or_container_isolation_not_implemented",
            implemented=False,
        ),
    ]


def live_hostile_isolation_drill_receipts() -> list[dict[str, Any]]:
    return [
        _hostile_drill(
            "ck-hostile-prompt-injection",
            "prompt_injection",
            ["ignore_policy_instruction", "exfiltrate_memory", "silent_delegation_policy_lift"],
            "blocked",
            ["quarantine_prompt_surface", "require_human_review", "preserve_redacted_transcript"],
        ),
        _hostile_drill(
            "ck-hostile-ssrf-private-egress",
            "ssrf_private_egress",
            ["loopback_fetch", "rfc1918_redirect", "metadata_service_probe"],
            "blocked",
            ["deny_egress", "record_site_policy_receipt", "require_private_network_policy_review"],
        ),
        _hostile_drill(
            "ck-hostile-filesystem-escape",
            "filesystem_escape",
            ["path_traversal", "secret_like_file_read", "symlink_escape_candidate"],
            "blocked",
            ["deny_path", "record_workspace_boundary_receipt"],
        ),
        _hostile_drill(
            "ck-hostile-credential-exfiltration",
            "credential_exfiltration",
            ["raw_secret_echo", "cross_session_secret_ref_replay", "destination_host_drift"],
            "blocked",
            ["redact_output", "rotate_secret_ref", "notify_operator"],
        ),
        _hostile_drill(
            "ck-hostile-extension-permission-creep",
            "extension_permission_creep",
            ["new_network_permission", "undeclared_browser_provider", "publisher_key_rotation_without_review"],
            "quarantined",
            ["rollback_extension", "mark_update_review_required", "disable_runtime_contribution"],
        ),
        _hostile_drill(
            "ck-hostile-replay-approval-drift",
            "workflow_replay_approval_drift",
            ["approval_scope_expansion", "different_operator_context", "provider_trust_class_lift"],
            "blocked",
            ["resume_from_checkpoint", "request_fresh_approval", "preserve_original_scope"],
        ),
        _hostile_drill(
            "ck-hostile-browser-session-bleed",
            "browser_session_bleed",
            ["owner_session_mismatch", "cookie_state_reuse", "download_handle_cross_run_replay"],
            "blocked",
            ["create_fresh_browser_context", "drop_profile_state", "redact_download_receipt"],
        ),
    ]


def secure_host_recovery_authority_receipts() -> list[dict[str, Any]]:
    return [
        _recovery_authority("allow", "low_risk_read_only_receipt", "operator_can_allow_with_scope_receipt", False),
        _recovery_authority("deny", "untrusted_privileged_action", "deny_and_preserve_audit", False),
        _recovery_authority("quarantine", "suspicious_extension_or_prompt_surface", "quarantine_and_hide_runtime_contribution", True),
        _recovery_authority("rotate", "credential_boundary_drift", "rotate_secret_ref_before_retry", True),
        _recovery_authority("recover", "blocked_but_repairable_workflow_replay", "recover_from_verified_checkpoint", True),
        _recovery_authority("post_incident_audit", "security_boundary_incident", "emit_reviewable_post_incident_audit", False),
    ]


def build_independent_secure_host_review_contract() -> dict[str, Any]:
    review_receipts = independent_security_review_receipts()
    isolation_matrix = isolation_evidence_matrix()
    hostile_drills = live_hostile_isolation_drill_receipts()
    recovery_receipts = secure_host_recovery_authority_receipts()
    findings = [
        finding
        for receipt in review_receipts
        for finding in receipt.get("findings", [])
        if isinstance(finding, dict)
    ]
    blocked_or_quarantined = {"blocked", "quarantined"}
    return {
        "summary": {
            "suite_name": "independent_secure_host_review",
            "operator_status": "independent_secure_host_review_receipts_visible",
            "review_receipt_count": len(review_receipts),
            "reviewed_surface_count": len(review_receipts[0]["target_surfaces"]),
            "finding_count": len(findings),
            "remediated_or_documented_finding_count": sum(
                1
                for item in findings
                if item["status"] in {"remediated", "residual_risk_documented"}
            ),
            "isolation_evidence_count": len(isolation_matrix),
            "implemented_isolation_evidence_count": sum(1 for item in isolation_matrix if item["implemented"] is True),
            "unsupported_isolation_claim_visible": any(item["implemented"] is False for item in isolation_matrix),
            "hostile_drill_count": len(hostile_drills),
            "hostile_drill_fail_closed_count": sum(
                1 for item in hostile_drills if item["policy_decision"] in blocked_or_quarantined
            ),
            "recovery_authority_count": len(recovery_receipts),
            "operator_recovery_actions_visible": {"allow", "deny", "quarantine", "rotate", "recover", "post_incident_audit"}
            <= {item["action"] for item in recovery_receipts},
            "claim_boundary": INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
        },
        "review_receipts": review_receipts,
        "isolation_evidence_matrix": isolation_matrix,
        "hostile_drill_receipts": hostile_drills,
        "recovery_authority_receipts": recovery_receipts,
        "policy": independent_secure_host_review_policy_payload(),
    }


def _review_finding(finding_id: str, severity: str, surface: str, status: str) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "surface": surface,
        "status": status,
        "operator_visible": True,
        "residual_risk": INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
    }


def _isolation_evidence(
    surface: str,
    isolation_boundary: str,
    evidence_mode: str,
    residual_risk: str,
    *,
    implemented: bool = True,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "isolation_boundary": isolation_boundary,
        "evidence_mode": evidence_mode,
        "implemented": implemented,
        "operator_visible": True,
        "residual_risk": residual_risk,
        "blocked_claim_boundary": INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
    }


def _hostile_drill(
    drill_id: str,
    attack_family: str,
    attack_vectors: list[str],
    policy_decision: str,
    recovery_actions: list[str],
) -> dict[str, Any]:
    return {
        "drill_id": drill_id,
        "evidence_mode": "live_hostile_replay_fixture",
        "attack_family": attack_family,
        "attack_vectors": attack_vectors,
        "policy_decision": policy_decision,
        "fail_closed": policy_decision in {"blocked", "quarantined"},
        "recovery_actions": recovery_actions,
        "operator_visible": True,
        "post_incident_audit": f"audit:{drill_id}",
        "residual_uncertainty": INDEPENDENT_SECURE_HOST_REVIEW_CLAIM_BOUNDARY,
    }


def _recovery_authority(
    action: str,
    incident_type: str,
    authority_result: str,
    requires_review: bool,
) -> dict[str, Any]:
    return {
        "action": action,
        "incident_type": incident_type,
        "authority_result": authority_result,
        "requires_review": requires_review,
        "receipt_after_action": f"operator-control:{action}:independent-secure-host-review",
        "operator_visible": True,
    }


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "type": "benchmark_regression",
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Independent secure-host review scenario failed."),
            "reason": "independent_secure_host_review_eval_failure",
        })
    return failures[:10]


async def _run_independent_secure_host_review_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        INDEPENDENT_SECURE_HOST_REVIEW_SUITE_NAME,
        LIVE_HOSTILE_ISOLATION_DRILLS_SUITE_NAME,
        SECURE_HOST_RECOVERY_AUTHORITY_SUITE_NAME,
    ])


async def build_independent_secure_host_review_report() -> dict[str, Any]:
    summary = await _run_independent_secure_host_review_suites()
    contract = build_independent_secure_host_review_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "independent_secure_host_review_ci_gated_operator_visible"
                if healthy
                else "independent_secure_host_review_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES)
                + len(LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES)
                + len(SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            INDEPENDENT_SECURE_HOST_REVIEW_SUITE_NAME: list(INDEPENDENT_SECURE_HOST_REVIEW_SCENARIO_NAMES),
            LIVE_HOSTILE_ISOLATION_DRILLS_SUITE_NAME: list(LIVE_HOSTILE_ISOLATION_DRILLS_SCENARIO_NAMES),
            SECURE_HOST_RECOVERY_AUTHORITY_SUITE_NAME: list(SECURE_HOST_RECOVERY_AUTHORITY_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
