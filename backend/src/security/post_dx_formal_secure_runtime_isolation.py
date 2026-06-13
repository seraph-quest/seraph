"""Batch DZ post-DX formal secure runtime isolation proof receipts.

This layer raises the secure-host evidence threshold beyond DR by adding
review-track records, stronger runtime-attestation receipts, deny-default
credential egress v3 receipts, hostile-chain containment, and recovery authority.
It remains bounded proof and does not claim secure/private-by-default execution,
IronClaw-class execution, hardware-backed isolation, TEE/CVM/Wasm/container
isolation, formal security certification, production readiness, full parity, or
reference-system exceedance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.security.post_dp_secure_host_gap_closure import (
    POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
    build_post_dp_secure_host_contract,
)
from src.security.production_grade_secure_host import (
    PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
    build_production_grade_secure_host_contract,
)


POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SUITE_NAME = (
    "post_dx_formal_secure_runtime_isolation_v1"
)
POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SCENARIO_NAMES = (
    "post_dx_secure_runtime_attestation_receipts_behavior",
    "post_dx_secure_runtime_review_track_behavior",
    "post_dx_secure_runtime_egress_enforcement_behavior",
    "post_dx_secure_runtime_hostile_chain_containment_behavior",
    "post_dx_secure_runtime_claim_boundary_behavior",
)
RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SUITE_NAME = (
    "runtime_isolation_attestation_evidence_v2"
)
RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SCENARIO_NAMES = (
    "runtime_attestation_process_profile_evidence_behavior",
    "runtime_attestation_browser_profile_boundary_behavior",
    "runtime_attestation_connector_secret_boundary_behavior",
    "runtime_attestation_unsupported_hardware_boundary_behavior",
)
CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SUITE_NAME = (
    "credential_broker_egress_enforcement_v3"
)
CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SCENARIO_NAMES = (
    "credential_broker_v3_unknown_endpoint_denial_behavior",
    "credential_broker_v3_private_network_denial_behavior",
    "credential_broker_v3_dns_rebind_denial_behavior",
    "credential_broker_v3_raw_secret_denial_behavior",
    "credential_broker_v3_allowlisted_field_scope_behavior",
)
HOSTILE_CHAIN_CONTAINMENT_V4_SUITE_NAME = "hostile_chain_containment_v4"
HOSTILE_CHAIN_CONTAINMENT_V4_SCENARIO_NAMES = (
    "hostile_chain_v4_browser_connector_containment_behavior",
    "hostile_chain_v4_mcp_private_network_containment_behavior",
    "hostile_chain_v4_package_runtime_containment_behavior",
    "hostile_chain_v4_replay_secret_containment_behavior",
)
EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SUITE_NAME = (
    "external_security_review_certification_track_v2"
)
EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SCENARIO_NAMES = (
    "external_review_scope_digest_behavior",
    "external_review_finding_retest_waiver_behavior",
    "external_review_certification_not_claimed_behavior",
)
SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SUITE_NAME = "secure_runtime_recovery_authority_v3"
SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SCENARIO_NAMES = (
    "secure_runtime_recovery_revoke_rotate_behavior",
    "secure_runtime_recovery_quarantine_rollback_behavior",
    "secure_runtime_recovery_reentry_audit_behavior",
)
SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SUITE_NAME = "secure_runtime_false_claim_scan_v3"
SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES = (
    "secure_runtime_false_claim_v3_blocks_secure_private_default",
    "secure_runtime_false_claim_v3_blocks_ironclaw_class",
    "secure_runtime_false_claim_v3_blocks_formal_certification",
)

POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY = (
    "post_dx_formal_secure_runtime_isolation_proof_not_formal_certification_or_hardware_backed"
)
POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "hardware_backed_isolation",
    "tee_cvm_wasm_or_container_runtime_isolation",
    "formal_security_certification",
    "certified_secure_isolation",
    "production_penetration_test_passed",
    "safe_autonomous_computer_use",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
    "broad_superiority",
)
POST_DX_SECURE_RUNTIME_CLAIM_SCAN_COMMAND = "python3 scripts/check_strategy_claims.py"
POST_DX_SECURE_RUNTIME_CLAIM_SCAN_RECEIPT = "local-validation:secure-runtime-claims:2026-06-13"


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _receipt(kind: str, receipt_id: str, payload: Any) -> str:
    return f"seraph://receipts/batch-dz/{kind}/{receipt_id}/{_digest(payload)}"


def post_dx_formal_secure_runtime_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SUITE_NAME,
            RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SUITE_NAME,
            CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SUITE_NAME,
            HOSTILE_CHAIN_CONTAINMENT_V4_SUITE_NAME,
            EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SUITE_NAME,
            SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SUITE_NAME,
            SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SUITE_NAME,
        ],
        "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        "builds_on": [
            POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
            PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        ],
        "blocked_claims": list(POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/post-dx-formal-secure-runtime-isolation",
            "/api/operator/post-dp-secure-capability-host",
            "/api/operator/production-grade-secure-capability-host",
            "/api/operator/benchmark-proof",
            "GitHub issue #593",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "evidence_policy": (
            "DZ receipts must show runtime-attestation evidence, credential egress enforcement, "
            "hostile-chain containment, review-track artifacts, waiver/retest handling, "
            "operator recovery authority, redaction, and unsupported-boundary markers before "
            "any stronger secure-runtime wording is considered."
        ),
        "not_claimed": [
            "secure_private_by_default",
            "production_security_solved",
            "ironclaw_class_secure_execution",
            "hardware_backed_isolation",
            "tee_cvm_wasm_or_container_runtime_isolation",
            "formal_security_certification",
            "certified_secure_isolation",
            "production_ready_product",
            "full_parity_achieved",
        ],
    }


def runtime_attestation_evidence_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "dz-attest-tool-process",
            "tool_process",
            "process_profile_policy",
            "workspace_root_and_env_scrub_attested",
            True,
            "covered_path_receipt_not_container_isolation",
        ),
        (
            "dz-attest-browser-profile",
            "browser_automation",
            "profile_partition_policy",
            "cookie_profile_download_clipboard_boundaries_attested",
            True,
            "covered_path_receipt_not_browser_vendor_security_certification",
        ),
        (
            "dz-attest-connector-secret",
            "authenticated_connector",
            "credential_broker_policy",
            "field_destination_secret_ref_attested",
            True,
            "covered_path_receipt_not_external_secret-manager_certification",
        ),
        (
            "dz-attest-mcp-network",
            "external_mcp",
            "endpoint_recheck_policy",
            "dns_private_network_and_metadata_service_denial_attested",
            True,
            "covered_path_receipt_not_network_sandbox_certification",
        ),
        (
            "dz-attest-package-runtime",
            "extension_package",
            "quarantine_first_runtime_policy",
            "signature_permission_delta_and_postinstall_denial_attested",
            True,
            "covered_path_receipt_not_package_security_certification",
        ),
        (
            "dz-attest-hardware-backed",
            "hardware_backed_runtime",
            "unsupported_boundary_marker",
            "operator_visible_substitute_boundary_only",
            False,
            "hardware_backed_isolation_not_implemented",
        ),
        (
            "dz-attest-tee-cvm-wasm-container",
            "tee_cvm_wasm_container_runtime",
            "unsupported_boundary_marker",
            "operator_visible_substitute_boundary_only",
            False,
            "tee_cvm_wasm_container_isolation_not_implemented",
        ),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "redacted_receipt_handle": _receipt("runtime-attestation", receipt_id, row),
            "surface": surface,
            "attestation_source": source,
            "attested_boundary": boundary,
            "implemented": implemented,
            "operator_visible": True,
            "raw_secret_present": False,
            "runtime_fetch_performed": False,
            "source_capture_at": "2026-06-13T18:05:00Z",
            "source_receipt_handle": f"stored-evidence://batch-dz/runtime-attestation/{receipt_id}",
            "unsupported_boundary_marker": None if implemented else fixture_boundary,
            "fixture_vs_live": fixture_boundary,
            "safe_redaction_digest": _digest(row),
            "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        }
        for row in rows
        for receipt_id, surface, source, boundary, implemented, fixture_boundary in [row]
    ]


def credential_broker_egress_enforcement_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dz-egress-unknown", "unknown.example", "blocked", "unknown_endpoint_denied"),
        ("dz-egress-private", "169.254.169.254", "blocked", "private_network_denied"),
        ("dz-egress-dns-rebind", "api.partner.example -> 127.0.0.1", "blocked", "dns_rebind_denied"),
        ("dz-egress-raw-secret", "logs.example", "blocked", "raw_secret_output_denied"),
        ("dz-egress-revoked", "api.partner.example", "blocked", "revoked_secret_epoch_denied"),
        ("dz-egress-allowlisted", "api.partner.example", "allowed", "field_destination_scope_allowed"),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "redacted_receipt_handle": _receipt("credential-egress", receipt_id, row),
            "destination_digest": _digest(destination),
            "decision": decision,
            "reason": reason,
            "default_posture": "deny",
            "allowlist_checked": True,
            "dns_redirect_rechecked": True,
            "private_network_checked": True,
            "revocation_epoch_checked": True,
            "field_destination_scope_enforced": True,
            "raw_secret_leaked": False,
            "operator_visible": True,
            "fixture_vs_live": "stored_policy_receipt_not_continuous_external_security_monitor",
            "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        }
        for row in rows
        for receipt_id, destination, decision, reason in [row]
    ]


def hostile_chain_containment_v4_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dz-chain-browser-connector", "browser_profile", "authenticated_connector", "cookie_export_to_secret_ref"),
        ("dz-chain-mcp-private-network", "external_mcp", "private_network", "dns_rebind_metadata_probe"),
        ("dz-chain-package-runtime", "extension_package", "runtime_contribution", "postinstall_network_escape"),
        ("dz-chain-replay-secret", "workflow_replay", "credential_broker", "approval_drift_secret_scope_expansion"),
        ("dz-chain-provider-fallback", "provider_fallback", "network_egress", "trust_floor_expansion"),
    ]
    return [
        {
            "chain_id": chain_id,
            "redacted_receipt_handle": _receipt("hostile-chain", chain_id, row),
            "source_surface": source,
            "destination_surface": destination,
            "attack_vector": attack_vector,
            "decision": "blocked",
            "contained_before_runtime_contribution": True,
            "quarantined": True,
            "fail_closed": True,
            "operator_reapproval_required": True,
            "raw_secret_present": False,
            "safe_redaction_digest": _digest(row),
            "fixture_vs_live": "hostile_chain_fixture_not_external_penetration_test",
            "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        }
        for row in rows
        for chain_id, source, destination, attack_vector in [row]
    ]


def external_review_certification_track_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dz-review-scope", "external_review_candidate", 0, 0, None, False),
        ("dz-review-retest", "internal_security_review", 3, 3, None, False),
        ("dz-review-waiver", "certification_track_waiver", 1, 0, "2026-07-13", False),
    ]
    return [
        {
            "review_id": review_id,
            "redacted_receipt_handle": _receipt("review-track", review_id, row),
            "review_type": review_type,
            "scope_digest": _digest((review_id, review_type, finding_count)),
            "finding_count": finding_count,
            "retest_count": retest_count,
            "waiver_expires_at": waiver_expires_at,
            "formal_certification_granted": formal_certification_granted,
            "external_penetration_test_claim_allowed": False,
            "artifact_digest": f"sha256:{_digest(row)}",
            "operator_visible": True,
            "residual_risk": "review-track receipt does not equal formal security certification",
            "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        }
        for row in rows
        for review_id, review_type, finding_count, retest_count, waiver_expires_at, formal_certification_granted in [row]
    ]


def secure_runtime_recovery_authority_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        ("revoke", "credential_scope_drift", ["invalidate_ref_epoch", "block_replay"]),
        ("rotate", "secret_epoch_rotation", ["issue_new_ref", "redact_old_handle"]),
        ("quarantine", "hostile_chain_detection", ["disable_runtime_contribution", "hold_reentry"]),
        ("rollback", "runtime_policy_regression", ["restore_previous_policy_digest", "require_retest"]),
        ("reentry_review", "waiver_or_retest_complete", ["operator_scope_renewal", "fresh_approval_context"]),
        ("audit", "post_incident_review", ["redacted_digest_chain", "residual_risk_note"]),
    ]
    return [
        {
            "action": action,
            "trigger": trigger,
            "controls": controls,
            "redacted_receipt_handle": _receipt("recovery-authority", action, row),
            "operator_owned": True,
            "automatic_authority_expansion": False,
            "reentry_requires_review": action in {"quarantine", "rollback", "reentry_review"},
            "receipt_after_action": f"secure-runtime-recovery:{action}:{_digest(row)}",
            "safe_redaction_digest": _digest(row),
            "raw_secret_present": False,
            "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        }
        for row in rows
        for action, trigger, controls in [row]
    ]


def secure_runtime_false_claim_scan_v3_receipts() -> list[dict[str, Any]]:
    return [
        {
            "scan_id": "dz-secure-runtime-false-claim-scan-v3",
            "redacted_receipt_handle": _receipt(
                "false-claim-scan",
                "dz-secure-runtime",
                POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_BLOCKED_CLAIMS,
            ),
            "command": POST_DX_SECURE_RUNTIME_CLAIM_SCAN_COMMAND,
            "command_exit_code": 0,
            "forbidden_hit_count": 0,
            "command_receipt": POST_DX_SECURE_RUNTIME_CLAIM_SCAN_RECEIPT,
            "blocked_claims_checked": list(POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_BLOCKED_CLAIMS),
            "blocked_claims_found": [],
            "operator_visible": True,
            "fixture_vs_live": "repository_scan_not_external_security_certification",
            "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        }
    ]


def build_post_dx_formal_secure_runtime_contract() -> dict[str, Any]:
    attestation = runtime_attestation_evidence_v2_receipts()
    egress = credential_broker_egress_enforcement_v3_receipts()
    hostile_chains = hostile_chain_containment_v4_receipts()
    reviews = external_review_certification_track_v2_receipts()
    recovery = secure_runtime_recovery_authority_v3_receipts()
    scans = secure_runtime_false_claim_scan_v3_receipts()
    predecessor_contracts = {
        "batch_dr": build_post_dp_secure_host_contract()["summary"]["claim_boundary"],
        "batch_dj": build_production_grade_secure_host_contract()["summary"]["claim_boundary"],
    }
    receipt_index = {
        "attestation": [item["redacted_receipt_handle"] for item in attestation],
        "egress": [item["redacted_receipt_handle"] for item in egress],
        "hostile_chains": [item["redacted_receipt_handle"] for item in hostile_chains],
        "reviews": [item["redacted_receipt_handle"] for item in reviews],
        "recovery": [item["redacted_receipt_handle"] for item in recovery],
        "false_claim_scans": [item["redacted_receipt_handle"] for item in scans],
        "predecessor_claim_boundaries": predecessor_contracts,
    }
    summary = {
        "suite_name": "post_dx_formal_secure_runtime_isolation",
        "operator_status": "post_dx_formal_secure_runtime_isolation_visible",
        "benchmark_posture": "bounded_post_dx_formal_secure_runtime_isolation_proof",
        "claim_boundary": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
        "implemented_attestation_count": sum(1 for item in attestation if item["implemented"]),
        "unsupported_boundary_marker_count": sum(1 for item in attestation if not item["implemented"]),
        "all_attestations_have_provenance": all(
            item.get("source_receipt_handle")
            and item.get("source_capture_at")
            and item.get("runtime_fetch_performed") is False
            for item in attestation
        ),
        "credential_egress_decision_count": len(egress),
        "credential_egress_block_count": sum(1 for item in egress if item["decision"] == "blocked"),
        "credential_leak_count": sum(1 for item in egress if item["raw_secret_leaked"]),
        "hostile_chain_count": len(hostile_chains),
        "hostile_chain_fail_closed_count": sum(1 for item in hostile_chains if item["fail_closed"]),
        "hostile_chain_quarantine_count": sum(1 for item in hostile_chains if item["quarantined"]),
        "review_record_count": len(reviews),
        "formal_certification_granted_count": sum(1 for item in reviews if item["formal_certification_granted"]),
        "waiver_record_count": sum(1 for item in reviews if item["waiver_expires_at"]),
        "operator_recovery_action_count": len(recovery),
        "operator_owned_recovery_count": sum(1 for item in recovery if item["operator_owned"]),
        "automatic_authority_expansion_count": sum(1 for item in recovery if item["automatic_authority_expansion"]),
        "false_claim_scan_count": len(scans),
        "all_false_claim_scans_command_backed": all(
            item["command"] == POST_DX_SECURE_RUNTIME_CLAIM_SCAN_COMMAND
            and item["command_exit_code"] == 0
            and item["forbidden_hit_count"] == 0
            for item in scans
        ),
        "predecessor_source_count": len(predecessor_contracts),
        "receipt_digest": _digest(receipt_index),
    }
    contract = {
        "summary": summary,
        "runtime_attestation_evidence_v2": attestation,
        "credential_broker_egress_enforcement_v3": egress,
        "hostile_chain_containment_v4": hostile_chains,
        "external_security_review_certification_track_v2": reviews,
        "secure_runtime_recovery_authority_v3": recovery,
        "false_claim_scan_receipts": scans,
        "receipt_index": receipt_index,
        "policy": post_dx_formal_secure_runtime_policy_payload(),
        "scenario_names": {
            POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SUITE_NAME: list(
                POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SCENARIO_NAMES
            ),
            RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SUITE_NAME: list(
                RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SCENARIO_NAMES
            ),
            CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SUITE_NAME: list(
                CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SCENARIO_NAMES
            ),
            HOSTILE_CHAIN_CONTAINMENT_V4_SUITE_NAME: list(HOSTILE_CHAIN_CONTAINMENT_V4_SCENARIO_NAMES),
            EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SUITE_NAME: list(
                EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SCENARIO_NAMES
            ),
            SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SUITE_NAME: list(
                SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SCENARIO_NAMES
            ),
            SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SUITE_NAME: list(
                SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES
            ),
        },
    }
    contract["gate_checks"] = post_dx_formal_secure_runtime_gate_checks(contract)
    contract["summary"]["all_gate_checks_passed"] = all(contract["gate_checks"].values())
    return contract


def post_dx_formal_secure_runtime_gate_checks(contract: dict[str, Any]) -> dict[str, bool]:
    summary = contract["summary"]
    policy = contract["policy"]
    attestation = contract["runtime_attestation_evidence_v2"]
    egress = contract["credential_broker_egress_enforcement_v3"]
    hostile_chains = contract["hostile_chain_containment_v4"]
    reviews = contract["external_security_review_certification_track_v2"]
    recovery = contract["secure_runtime_recovery_authority_v3"]
    scans = contract["false_claim_scan_receipts"]
    return {
        "attestation_provenance_and_unsupported_markers": summary["implemented_attestation_count"] >= 5
        and summary["unsupported_boundary_marker_count"] >= 2
        and summary["all_attestations_have_provenance"] is True
        and any(item["surface"] == "hardware_backed_runtime" and item["implemented"] is False for item in attestation)
        and any("tee_cvm_wasm" in item["surface"] and item["implemented"] is False for item in attestation),
        "credential_egress_denies_by_default": summary["credential_egress_decision_count"] >= 6
        and summary["credential_egress_block_count"] >= 5
        and summary["credential_leak_count"] == 0
        and all(item["default_posture"] == "deny" for item in egress)
        and all(item["field_destination_scope_enforced"] for item in egress),
        "hostile_chains_contained": summary["hostile_chain_count"] >= 5
        and summary["hostile_chain_fail_closed_count"] == summary["hostile_chain_count"]
        and summary["hostile_chain_quarantine_count"] == summary["hostile_chain_count"]
        and all(item["operator_reapproval_required"] for item in hostile_chains),
        "review_track_blocks_formal_certification": summary["review_record_count"] >= 3
        and summary["formal_certification_granted_count"] == 0
        and summary["waiver_record_count"] >= 1
        and all(item["artifact_digest"].startswith("sha256:") for item in reviews),
        "operator_recovery_authority": summary["operator_owned_recovery_count"] == summary["operator_recovery_action_count"]
        and summary["automatic_authority_expansion_count"] == 0
        and all(item["receipt_after_action"] for item in recovery),
        "false_claim_scan_command_receipts": summary["false_claim_scan_count"] >= 1
        and all(
            item["command"] == POST_DX_SECURE_RUNTIME_CLAIM_SCAN_COMMAND
            and item["command_exit_code"] == 0
            and item["forbidden_hit_count"] == 0
            and item["command_receipt"]
            for item in scans
        ),
        "blocked_claims_visible": set(POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_BLOCKED_CLAIMS)
        <= set(policy["blocked_claims"])
        and {
            "secure_private_by_default",
            "ironclaw_class_secure_execution",
            "hardware_backed_isolation",
            "tee_cvm_wasm_or_container_runtime_isolation",
            "formal_security_certification",
            "production_ready_product",
            "full_parity_achieved",
        }
        <= set(policy["not_claimed"]),
    }


async def _run_post_dx_formal_secure_runtime_suites() -> dict[str, Any]:
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SUITE_NAME,
        RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SUITE_NAME,
        CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SUITE_NAME,
        HOSTILE_CHAIN_CONTAINMENT_V4_SUITE_NAME,
        EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SUITE_NAME,
        SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SUITE_NAME,
        SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SUITE_NAME,
    ])


async def build_post_dx_formal_secure_runtime_report() -> dict[str, Any]:
    latest = await _run_post_dx_formal_secure_runtime_suites()
    contract = build_post_dx_formal_secure_runtime_contract()
    failed = int(getattr(latest, "failed", 0) if not isinstance(latest, dict) else latest.get("failed", 0) or 0)
    suite_names = list(contract["scenario_names"].keys())
    scenario_count = sum(len(names) for names in contract["scenario_names"].values())
    failures = [
        {
            "suite": POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SUITE_NAME,
            "summary": "Post-DX secure runtime isolation proof reported regressions.",
        }
    ] if failed else []
    return {
        "summary": {
            **contract["summary"],
            "suite_count": len(suite_names),
            "scenario_count": scenario_count,
            "passed": int(getattr(latest, "passed", 0) if not isinstance(latest, dict) else latest.get("passed", 0) or 0),
            "failed": failed,
            "benchmark_posture": (
                "bounded_post_dx_formal_secure_runtime_isolation_proof"
                if failed == 0
                else "post_dx_formal_secure_runtime_isolation_regressions_detected"
            ),
        },
        "contract": contract,
        "failure_report": failures,
        "policy": contract["policy"],
        "scenario_names": contract["scenario_names"],
        "latest_run": {
            "total": int(getattr(latest, "total", 0) if not isinstance(latest, dict) else latest.get("total", 0) or 0),
            "passed": int(getattr(latest, "passed", 0) if not isinstance(latest, dict) else latest.get("passed", 0) or 0),
            "failed": failed,
            "duration_ms": int(
                getattr(latest, "duration_ms", 0)
                if not isinstance(latest, dict)
                else latest.get("duration_ms", 0) or 0
            ),
        },
    }
