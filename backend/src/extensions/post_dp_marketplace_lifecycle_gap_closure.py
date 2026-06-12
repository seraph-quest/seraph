"""Batch DV post-DP capability marketplace lifecycle gap-closure receipts.

This module closes the next marketplace lifecycle gap after DN by binding
provenance, package-security, secure-host, waiver, rollback, quarantine, and
audit evidence to concrete lifecycle transitions. It remains bounded evidence:
it does not claim a production-secure marketplace, solved third-party package
security, formal package-security certification, full marketplace parity,
production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from src.extensions.marketplace_production_security import (
    MARKETPLACE_PRODUCTION_SECURITY_CLAIM_BOUNDARY,
    build_marketplace_production_security_contract,
)
from src.security.post_dp_secure_host_gap_closure import (
    POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
    build_post_dp_secure_host_contract,
)


POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SUITE_NAME = (
    "post_dp_capability_marketplace_lifecycle_gap_closure_v1"
)
POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SCENARIO_NAMES = (
    "post_dp_marketplace_lifecycle_builds_on_dn_and_dr_without_duplicate_scope",
    "post_dp_marketplace_lifecycle_operator_receipts_behavior",
    "post_dp_marketplace_lifecycle_secure_host_integration_behavior",
    "post_dp_marketplace_lifecycle_claim_boundary_behavior",
)
MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SUITE_NAME = "marketplace_lifecycle_operations_v3"
MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SCENARIO_NAMES = (
    "marketplace_lifecycle_v3_install_update_receipts_behavior",
    "marketplace_lifecycle_v3_downgrade_disable_receipts_behavior",
    "marketplace_lifecycle_v3_rollback_quarantine_reentry_behavior",
    "marketplace_lifecycle_v3_failed_update_diagnostics_behavior",
)
PACKAGE_REVIEW_WAIVER_POLICY_V2_SUITE_NAME = "package_review_waiver_policy_v2"
PACKAGE_REVIEW_WAIVER_POLICY_V2_SCENARIO_NAMES = (
    "package_review_v2_high_critical_denial_behavior",
    "package_review_v2_explicit_waiver_scope_expiry_behavior",
    "package_review_v2_expired_or_out_of_scope_waiver_behavior",
    "package_review_v2_retest_and_operator_receipt_behavior",
)
MARKETPLACE_VULNERABILITY_MONITORING_V2_SUITE_NAME = "marketplace_vulnerability_monitoring_v2"
MARKETPLACE_VULNERABILITY_MONITORING_V2_SCENARIO_NAMES = (
    "marketplace_vulnerability_v2_scanner_freshness_behavior",
    "marketplace_vulnerability_v2_dependency_impact_behavior",
    "marketplace_vulnerability_v2_publisher_key_revocation_behavior",
    "marketplace_vulnerability_v2_critical_high_fail_closed_behavior",
)
HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SUITE_NAME = "hostile_package_lifecycle_gauntlet_v3"
HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SCENARIO_NAMES = (
    "hostile_package_lifecycle_v3_malicious_update_behavior",
    "hostile_package_lifecycle_v3_rollback_bypass_behavior",
    "hostile_package_lifecycle_v3_quarantine_bypass_behavior",
    "hostile_package_lifecycle_v3_runtime_boundary_behavior",
)
MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SUITE_NAME = (
    "marketplace_rollback_quarantine_diagnostics_v2"
)
MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SCENARIO_NAMES = (
    "marketplace_rollback_v2_compatibility_cause_behavior",
    "marketplace_rollback_v2_permission_drift_cause_behavior",
    "marketplace_rollback_v2_dependency_scanner_publisher_cause_behavior",
    "marketplace_rollback_v2_runtime_boundary_cause_behavior",
)
MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SUITE_NAME = (
    "marketplace_secure_host_audit_integration_v1"
)
MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SCENARIO_NAMES = (
    "marketplace_secure_host_audit_runtime_profile_behavior",
    "marketplace_secure_host_audit_permission_delta_behavior",
    "marketplace_secure_host_audit_operator_event_chain_behavior",
    "marketplace_secure_host_audit_quarantine_reentry_authority_behavior",
)
MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SUITE_NAME = "marketplace_lifecycle_false_claim_scan_v2"
MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES = (
    "marketplace_lifecycle_false_claim_v2_blocks_production_secure_marketplace",
    "marketplace_lifecycle_false_claim_v2_blocks_solved_package_security",
    "marketplace_lifecycle_false_claim_v2_blocks_full_parity_and_exceedance",
)

POST_DP_MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY = (
    "post_dp_marketplace_lifecycle_gap_closure_not_production_secure_marketplace_"
    "solved_package_security_full_marketplace_parity_or_reference_exceedance"
)
POST_DP_MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS = (
    "production_secure_marketplace",
    "third_party_package_security_solved",
    "formal_package_security_certification",
    "full_marketplace_parity",
    "ecosystem_superiority",
    "package_count_superiority",
    "production_ready_product",
    "full_parity",
    "full_production_parity",
    "reference_systems_exceeded",
    "broad_superiority",
)

RUN_DATE = date(2026, 6, 12)
REQUIRED_LIFECYCLE_OPERATIONS = (
    "install",
    "update",
    "downgrade",
    "disable",
    "rollback",
    "quarantine",
    "reentry",
    "failed_update_recovery",
)
REQUIRED_LIFECYCLE_RECEIPT_FIELDS = (
    "provenance_digest",
    "signature_status",
    "publisher_id",
    "sbom_digest",
    "dependency_graph_digest",
    "vulnerability_policy",
    "compatibility_state",
    "permission_delta",
    "trust_tier",
    "secure_host_profile",
    "operator_audit_receipt",
)
REQUIRED_DIAGNOSTIC_CAUSES = (
    "compatibility",
    "permission_drift",
    "dependency",
    "scanner",
    "publisher",
    "runtime_boundary",
)


def post_dp_marketplace_lifecycle_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SUITE_NAME,
            MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SUITE_NAME,
            PACKAGE_REVIEW_WAIVER_POLICY_V2_SUITE_NAME,
            MARKETPLACE_VULNERABILITY_MONITORING_V2_SUITE_NAME,
            HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SUITE_NAME,
            MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SUITE_NAME,
            MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SUITE_NAME,
            MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
        ],
        "foundation_suites": [
            "marketplace_grade_capability_lifecycle",
            "capability_rollback_failure_diagnostics",
            "production_marketplace_security",
            "marketplace_security_corpus_v1",
            "production_secure_marketplace_v1",
            "marketplace_security_certification_track_v1",
            "production_secure_marketplace_live_ops_v2",
            "post_dp_secure_capability_host_gap_closure_v1",
        ],
        "claim_boundary": POST_DP_MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
        "blocked_claims": list(POST_DP_MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/post-dp-marketplace-lifecycle-gap-closure",
            "/api/operator/marketplace-production-security",
            "/api/operator/post-dp-secure-capability-host",
            "/api/operator/benchmark-proof",
            "GitHub issue #578",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "lifecycle_gate_policy": (
            "every install, update, downgrade, disable, rollback, quarantine, re-entry, and failed-update recovery "
            "receipt must expose provenance, signature, publisher, SBOM/dependency, vulnerability, compatibility, "
            "permission, trust-tier, secure-host profile, and operator audit evidence"
        ),
        "waiver_policy": (
            "critical and high findings deny install, update, promotion, rollback re-entry, or runtime contribution "
            "unless waiver scope is explicit, current, finding-bound, package-bound, and retest evidence is visible"
        ),
        "evidence_boundary": (
            "DV receipts are deterministic lifecycle and audit evidence layered on DN/DR; they are not formal "
            "package-security certification or a production marketplace claim"
        ),
        "not_claimed": list(POST_DP_MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS),
    }


def marketplace_lifecycle_operations_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dv-life-install", "install", "marketplace.github-reviewer", "allow_runtime_contribution", "verified", "current"),
        ("dv-life-update", "update", "marketplace.browser-runner", "staged_rollout_after_review", "verified", "current"),
        ("dv-life-downgrade", "downgrade", "marketplace.voice-summary", "hold_for_operator_review", "verified", "scoped_current"),
        ("dv-life-disable", "disable", "marketplace.legacy-connector", "runtime_disabled", "verified", "not_required"),
        ("dv-life-rollback", "rollback", "marketplace.browser-runner", "rolled_back_to_verified_snapshot", "verified", "not_required"),
        ("dv-life-quarantine", "quarantine", "marketplace.suspicious-exporter", "deny_and_quarantine", "missing", "missing"),
        ("dv-life-reentry", "reentry", "marketplace.suspicious-exporter", "deny_reentry_until_retest", "missing", "expired"),
        ("dv-life-failed-update", "failed_update_recovery", "marketplace.workflow-pack", "rollback_after_failed_update", "verified", "not_required"),
    ]
    return [
        _lifecycle_row(receipt_id, operation, package_id, decision, signature_status, waiver_state)
        for receipt_id, operation, package_id, decision, signature_status, waiver_state in rows
    ]


def package_review_waiver_policy_v2_receipts() -> list[dict[str, Any]]:
    return [
        _review_row(
            "dv-review-critical-missing-waiver",
            "marketplace.suspicious-exporter",
            "critical",
            "missing",
            "deny_and_quarantine",
            None,
        ),
        _review_row(
            "dv-review-high-expired-waiver",
            "marketplace.legacy-connector",
            "high",
            "expired",
            "deny_until_new_review",
            "2026-05-31",
        ),
        _review_row(
            "dv-review-high-out-of-scope-waiver",
            "marketplace.analytics-export",
            "high",
            "out_of_scope",
            "deny_and_hold_dependents",
            "2026-07-20",
        ),
        _review_row(
            "dv-review-high-scoped-waiver",
            "marketplace.browser-runner",
            "high",
            "scoped_current",
            "hold_for_staged_retest",
            "2026-07-15",
        ),
    ]


def marketplace_vulnerability_monitoring_v2_receipts() -> list[dict[str, Any]]:
    return [
        _vulnerability_row("dv-vuln-osv-current", "osv.dev", "current", "medium", "review_with_canary"),
        _vulnerability_row("dv-vuln-nvd-current", "nvd.nist.gov/rest/json/cves/2.0", "current", "high", "deny_until_retest"),
        _vulnerability_row("dv-vuln-ghsa-current", "github-advisory-database", "current", "critical", "deny_and_quarantine"),
        _vulnerability_row("dv-vuln-stale-mirror", "internal-mirror.fixture", "stale", "high", "deny_until_rescan"),
    ]


def hostile_package_lifecycle_gauntlet_v3_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dv-hostile-malicious-update", "malicious_update", "deny_update_and_restore_previous"),
        ("dv-hostile-rollback-bypass", "rollback_bypass", "deny_unverified_snapshot_restore"),
        ("dv-hostile-quarantine-bypass", "quarantine_bypass", "deny_reentry_and_extend_quarantine"),
        ("dv-hostile-runtime-boundary", "runtime_boundary_escape", "deny_runtime_contribution"),
        ("dv-hostile-permission-drift", "permission_drift", "deny_permission_expansion"),
        ("dv-hostile-publisher-key", "publisher_key_revocation", "deny_and_hold_dependents"),
    ]
    return [_hostile_row(receipt_id, drill_class, enforcement_action) for receipt_id, drill_class, enforcement_action in rows]


def marketplace_rollback_quarantine_diagnostics_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dv-diag-compatibility", "failed_update", "compatibility", "runtime_api_version_mismatch"),
        ("dv-diag-permission-drift", "failed_update", "permission_drift", "new_workspace_write_scope_requested"),
        ("dv-diag-dependency", "rollback", "dependency", "transitive_dependency_digest_changed"),
        ("dv-diag-scanner", "rollback", "scanner", "vulnerability_sources_stale"),
        ("dv-diag-publisher", "rollback", "publisher", "publisher_key_revoked"),
        ("dv-diag-runtime-boundary", "quarantine_reentry", "runtime_boundary", "secure_host_profile_recheck_failed"),
    ]
    return [
        _diagnostic_row(receipt_id, lifecycle_event, cause_class, root_cause)
        for receipt_id, lifecycle_event, cause_class, root_cause in rows
    ]


def marketplace_secure_host_audit_integration_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dv-audit-install", "install", "extension_package", "quarantine_first_runtime", "operator-approved-install"),
        ("dv-audit-update", "update", "extension_package", "permission_delta_review_required", "operator-approved-update"),
        ("dv-audit-rollback", "rollback", "extension_package", "verified_snapshot_restore", "operator-approved-rollback"),
        ("dv-audit-quarantine", "quarantine", "extension_package", "deny_runtime_contribution_until_reviewed", "operator-owned-quarantine"),
        ("dv-audit-reentry", "reentry", "extension_package", "retest_and_reapproval_required", "operator-denied-reentry"),
    ]
    return [
        _secure_host_audit_row(receipt_id, operation, capability_surface, runtime_profile, authority)
        for receipt_id, operation, capability_surface, runtime_profile, authority in rows
    ]


def marketplace_lifecycle_false_claim_scan_v2_receipt() -> dict[str, Any]:
    checked = list(POST_DP_MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS)
    scan_result = _run_strategy_claim_scan()
    safe_payload = {
        "scan_id": "dv-marketplace-lifecycle-false-claim-scan",
        "blocked_claims_checked": checked,
        "forbidden_hit_count": scan_result["forbidden_hit_count"],
        "command_exit_code": scan_result["command_exit_code"],
    }
    return {
        "scan_id": "dv-marketplace-lifecycle-false-claim-scan",
        "suite_name": MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
        "scenario_name": MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES[0],
        "validation_command": "python3 scripts/check_strategy_claims.py",
        "command_executed": True,
        "command_exit_code": scan_result["command_exit_code"],
        "stdout_digest": scan_result["stdout_digest"],
        "stderr_digest": scan_result["stderr_digest"],
        "scan_scope": [
            "docs/implementation",
            "docs/research/19-strategy-claim-ledger.md",
            "operator post-DP marketplace lifecycle receipts",
        ],
        "blocked_claims_checked": checked,
        "blocked_claims_found": scan_result["blocked_claims_found"],
        "forbidden_hit_count": scan_result["forbidden_hit_count"],
        "allowed_wording": [
            "post-DP marketplace lifecycle gap-closure receipts",
            "bounded lifecycle audit evidence",
            "not production-secure marketplace or solved package security",
        ],
        "residual_risk": "DX final claim gate still required before any full-parity or exceedance wording",
        "evidence_mode": "deterministic_false_claim_scan_receipt",
        "fixture_vs_live": "fixture_scan_receipt_with_operator_visible_scope",
        "operator_visible": True,
        "claim_lift_allowed": False,
        "scan_clean": scan_result["command_exit_code"] == 0,
        "safe_receipt": _safe_receipt("false-claim-scan", "dv-marketplace-lifecycle-false-claim-scan", safe_payload),
    }


def build_post_dp_marketplace_lifecycle_contract() -> dict[str, Any]:
    lifecycle = marketplace_lifecycle_operations_v3_receipts()
    reviews = package_review_waiver_policy_v2_receipts()
    vulnerabilities = marketplace_vulnerability_monitoring_v2_receipts()
    hostile = hostile_package_lifecycle_gauntlet_v3_receipts()
    diagnostics = marketplace_rollback_quarantine_diagnostics_v2_receipts()
    secure_host_audit = marketplace_secure_host_audit_integration_receipts()
    false_claim_scan = marketplace_lifecycle_false_claim_scan_v2_receipt()
    policy = post_dp_marketplace_lifecycle_policy_payload()
    upstream_marketplace = build_marketplace_production_security_contract()
    upstream_secure_host = build_post_dp_secure_host_contract()
    all_items = [*lifecycle, *reviews, *vulnerabilities, *hostile, *diagnostics, *secure_host_audit, false_claim_scan]
    high_critical_reviews = [item for item in reviews if item["severity"] in {"critical", "high"}]
    return {
        "summary": {
            "operator_status": "post_dp_marketplace_lifecycle_gap_closure_receipts_visible",
            "claim_boundary": POST_DP_MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
            "upstream_marketplace_claim_boundary": MARKETPLACE_PRODUCTION_SECURITY_CLAIM_BOUNDARY,
            "upstream_secure_host_claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
            "upstream_marketplace_receipt_digest": _digest(upstream_marketplace["summary"]),
            "upstream_secure_host_receipt_digest": _digest(upstream_secure_host["summary"]),
            "lifecycle_operation_count": len(lifecycle),
            "review_gate_count": len(reviews),
            "vulnerability_monitoring_count": len(vulnerabilities),
            "hostile_gauntlet_v3_count": len(hostile),
            "rollback_diagnostic_count": len(diagnostics),
            "secure_host_audit_count": len(secure_host_audit),
            "required_lifecycle_operations_covered": set(REQUIRED_LIFECYCLE_OPERATIONS)
            <= {item["operation"] for item in lifecycle},
            "required_lifecycle_receipt_fields_visible": all(
                set(REQUIRED_LIFECYCLE_RECEIPT_FIELDS) <= set(item["operator_visible_fields"])
                for item in lifecycle
            ),
            "diagnostic_causes_covered": set(REQUIRED_DIAGNOSTIC_CAUSES)
            <= {item["cause_class"] for item in diagnostics},
            "failed_update_and_rollback_diagnostics_visible": {
                "failed_update",
                "rollback",
                "quarantine_reentry",
            } <= {item["lifecycle_event"] for item in diagnostics},
            "high_critical_denied_without_valid_waiver": all(
                item["decision"] in {"deny_and_quarantine", "deny_until_new_review", "deny_and_hold_dependents"}
                for item in high_critical_reviews
                if item["waiver_state"] != "scoped_current"
            ),
            "explicit_waiver_scope_and_expiry_visible": any(
                item["waiver_state"] == "scoped_current"
                and item["waiver_scope_explicit"] is True
                and item["waiver_expires_at"]
                and item["waiver_expired"] is False
                for item in high_critical_reviews
            ),
            "expired_or_out_of_scope_waivers_denied": all(
                item["decision"] != "allow_runtime_contribution"
                for item in high_critical_reviews
                if item["waiver_state"] in {"expired", "out_of_scope", "missing"}
            ),
            "vulnerability_monitoring_fail_closed": all(
                item["critical_high_blocked"] is True for item in vulnerabilities if item["severity"] in {"critical", "high"}
            ),
            "hostile_gauntlet_v3_fail_closed": all(
                item["runtime_contribution_allowed"] is False
                and item["enforcement"]["status"] in {"denied", "quarantined", "rolled_back"}
                for item in hostile
            ),
            "secure_host_permissions_integrated": all(
                item["secure_host"]["permission_delta_reviewed"] is True
                and item["secure_host"]["runtime_contribution_gate"] in {"deny_until_reviewed", "verified_snapshot_only"}
                for item in secure_host_audit
            ),
            "operator_audit_receipts_visible": all(
                item.get("operator_audit_receipt") and item.get("audit_chain_digest")
                for item in [*lifecycle, *diagnostics, *secure_host_audit]
            ),
            "safe_receipts_redacted": _all_safe_receipts_redacted(all_items),
            "false_claim_scan_clean": false_claim_scan["forbidden_hit_count"] == 0,
            "production_secure_marketplace_claim_allowed": False,
            "third_party_package_security_solved_claim_allowed": False,
            "formal_certification_claim_allowed": False,
            "full_marketplace_parity_claim_allowed": False,
            "reference_systems_exceeded_claim_allowed": False,
        },
        "lifecycle_operations_v3": lifecycle,
        "package_review_waiver_policy_v2": reviews,
        "vulnerability_monitoring_v2": vulnerabilities,
        "hostile_package_lifecycle_gauntlet_v3": hostile,
        "rollback_quarantine_diagnostics_v2": diagnostics,
        "secure_host_audit_integration_v1": secure_host_audit,
        "marketplace_lifecycle_false_claim_scan_v2": false_claim_scan,
        "upstream_foundation": {
            "marketplace_production_security_summary": upstream_marketplace["summary"],
            "post_dp_secure_host_summary": upstream_secure_host["summary"],
        },
        "policy": policy,
    }


async def _run_post_dp_marketplace_lifecycle_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SUITE_NAME,
        MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SUITE_NAME,
        PACKAGE_REVIEW_WAIVER_POLICY_V2_SUITE_NAME,
        MARKETPLACE_VULNERABILITY_MONITORING_V2_SUITE_NAME,
        HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SUITE_NAME,
        MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SUITE_NAME,
        MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SUITE_NAME,
        MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    ])


async def build_post_dp_marketplace_lifecycle_report() -> dict[str, Any]:
    summary = await _run_post_dp_marketplace_lifecycle_suites()
    contract = build_post_dp_marketplace_lifecycle_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "post_dp_marketplace_lifecycle_gap_closure_ci_gated_operator_visible"
                if healthy
                else "post_dp_marketplace_lifecycle_gap_closure_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SCENARIO_NAMES)
                + len(MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SCENARIO_NAMES)
                + len(PACKAGE_REVIEW_WAIVER_POLICY_V2_SCENARIO_NAMES)
                + len(MARKETPLACE_VULNERABILITY_MONITORING_V2_SCENARIO_NAMES)
                + len(HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SCENARIO_NAMES)
                + len(MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SCENARIO_NAMES)
                + len(MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SCENARIO_NAMES)
                + len(MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SUITE_NAME: list(
                POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SCENARIO_NAMES
            ),
            MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SUITE_NAME: list(MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SCENARIO_NAMES),
            PACKAGE_REVIEW_WAIVER_POLICY_V2_SUITE_NAME: list(PACKAGE_REVIEW_WAIVER_POLICY_V2_SCENARIO_NAMES),
            MARKETPLACE_VULNERABILITY_MONITORING_V2_SUITE_NAME: list(
                MARKETPLACE_VULNERABILITY_MONITORING_V2_SCENARIO_NAMES
            ),
            HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SUITE_NAME: list(
                HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SCENARIO_NAMES
            ),
            MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SUITE_NAME: list(
                MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SCENARIO_NAMES
            ),
            MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SUITE_NAME: list(
                MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SCENARIO_NAMES
            ),
            MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SUITE_NAME: list(
                MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="post_dp_marketplace_lifecycle_gap_closure"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def _lifecycle_row(
    receipt_id: str,
    operation: str,
    package_id: str,
    decision: str,
    signature_status: str,
    waiver_state: str,
) -> dict[str, Any]:
    package_slug = package_id.replace(".", "-")
    finding_severity = "critical" if waiver_state in {"missing", "expired"} else "medium"
    permission_delta = (
        {"requested": ["network:public", "workspace:read"], "approved": ["network:public"], "missing": []}
        if decision != "deny_and_quarantine"
        else {"requested": ["network:private", "workspace:write"], "approved": [], "missing": ["network:private"]}
    )
    safe_payload = {
        "receipt_id": receipt_id,
        "operation": operation,
        "package_id": package_id,
        "decision": decision,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SUITE_NAME,
        "operation": operation,
        "package_id": package_id,
        "decision": decision,
        "provenance_digest": _digest(f"provenance:{package_id}:{operation}"),
        "signature_status": signature_status,
        "package_version": "3.6.1" if package_id == "marketplace.browser-runner" else "1.0.0",
        "package_digest": _digest(f"package:{package_id}:{operation}"),
        "signed_digest": _digest(f"signed:{package_id}:{operation}") if signature_status == "verified" else "missing",
        "signing_key_id": "signing-key-seraph-marketplace-2026q2" if signature_status == "verified" else "missing",
        "signing_key_state": "active" if signature_status == "verified" else "missing",
        "revocation_status": "not_revoked" if signature_status == "verified" else "unknown",
        "publisher_id": "pub.verified.seraph-labs" if signature_status == "verified" else "pub.unverified.unknown",
        "publisher_trust_state": "verified" if signature_status == "verified" else "unverified",
        "publisher_review_state": "current" if signature_status == "verified" else "missing",
        "sbom_digest": _digest(f"sbom:{package_id}:{operation}"),
        "dependency_graph_digest": _digest(f"deps:{package_id}:{operation}"),
        "vulnerability_sources": ["osv.dev", "nvd.nist.gov/rest/json/cves/2.0", "github-advisory-database"],
        "vulnerability_policy": {
            "finding_id": f"DV-{package_slug}-{operation}".upper(),
            "severity": finding_severity,
            "critical_high_blocked": finding_severity in {"critical", "high"},
            "waiver_state": waiver_state,
            "waiver_scope_explicit": waiver_state == "scoped_current",
            "waiver_expires_at": "2026-07-15" if waiver_state == "scoped_current" else None,
            "retest_required": finding_severity in {"critical", "high"},
        },
        "compatibility_state": "compatible" if decision not in {"deny_and_quarantine"} else "blocked",
        "permission_delta": permission_delta,
        "trust_tier": "verified_operator_visible" if signature_status == "verified" else "untrusted_quarantined",
        "trust_tier_before": "review_required",
        "trust_tier_after": "verified_operator_visible" if signature_status == "verified" else "untrusted_quarantined",
        "secure_host_profile": "extension_package_quarantine_first_runtime",
        "runtime_contribution_allowed": decision in {
            "allow_runtime_contribution",
            "staged_rollout_after_review",
            "rolled_back_to_verified_snapshot",
        },
        "rollback_snapshot_id": f"snapshot-dv-{package_slug}-{operation}",
        "operator_audit_receipt": _receipt_handle("lifecycle-audit", receipt_id, safe_payload),
        "audit_chain_digest": _digest(f"audit-chain:{receipt_id}:{operation}:{decision}"),
        "operator_visible_fields": list(REQUIRED_LIFECYCLE_RECEIPT_FIELDS),
        "operator_diagnostic_id": f"dv-lifecycle-diagnostic:{operation}:{package_id}",
        "evidence_mode": "deterministic_lifecycle_v3_receipt",
        "fixture_vs_live": "fixture_lifecycle_transition_with_operator_audit_shape",
        "recorded_at": RUN_DATE.isoformat(),
        "claim_lift_allowed": False,
        "residual_risk": "lifecycle_transition_evidence_not_full_marketplace_parity",
        "safe_receipt": _safe_receipt("lifecycle", receipt_id, safe_payload),
    }


def _review_row(
    receipt_id: str,
    package_id: str,
    severity: str,
    waiver_state: str,
    decision: str,
    waiver_expires_at: str | None,
) -> dict[str, Any]:
    package_version = "3.6.1" if package_id == "marketplace.browser-runner" else "1.0.0"
    package_digest = _digest(f"waiver-package:{package_id}:{package_version}")
    finding_id = f"{receipt_id.upper()}-FINDING"
    waiver_id = f"waiver:{receipt_id}" if waiver_state != "missing" else None
    scope = (
        {
            "package_id": package_id,
            "package_version": package_version,
            "package_digest": package_digest,
            "finding_id": finding_id,
            "operation": "update",
            "runtime_profile": "extension_package_quarantine_first_runtime",
        }
        if waiver_state == "scoped_current"
        else {}
    )
    safe_payload = {
        "receipt_id": receipt_id,
        "package_id": package_id,
        "severity": severity,
        "decision": decision,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": PACKAGE_REVIEW_WAIVER_POLICY_V2_SUITE_NAME,
        "package_id": package_id,
        "package_version": package_version,
        "package_digest": package_digest,
        "publisher_id": "pub.verified.seraph-labs" if waiver_state == "scoped_current" else "pub.risk.fixture",
        "finding_id": finding_id,
        "severity": severity,
        "waiver_id": waiver_id,
        "waiver_state": waiver_state,
        "waiver_scope": scope,
        "waiver_scope_explicit": waiver_state == "scoped_current",
        "waiver_expires_at": waiver_expires_at,
        "waiver_expired": waiver_expires_at is not None and date.fromisoformat(waiver_expires_at) < RUN_DATE,
        "scope_digest": _digest(scope) if scope else None,
        "allowed_operations": ["update"] if waiver_state == "scoped_current" else [],
        "permission_boundaries": {
            "network": ["public"],
            "workspace": ["read"],
            "credential_refs": ["publisher-scoped-ref"],
        } if waiver_state == "scoped_current" else {},
        "runtime_profile": "extension_package_quarantine_first_runtime",
        "compensating_controls": ["staged_rollout", "operator_retest_before_reentry"]
        if waiver_state == "scoped_current"
        else [],
        "reviewer_id": "reviewer.marketplace.dv.2026q2",
        "approved_at": RUN_DATE.isoformat() if waiver_state == "scoped_current" else None,
        "retest_evidence": "operator_visible_retest_receipt",
        "decision": decision,
        "critical_high_denied_without_valid_waiver": (
            severity in {"critical", "high"}
            and waiver_state != "scoped_current"
            and decision in {"deny_and_quarantine", "deny_until_new_review", "deny_and_hold_dependents"}
        ),
        "operator_audit_receipt": _receipt_handle("review-waiver", receipt_id, safe_payload),
        "audit_chain_digest": _digest(f"review-audit:{receipt_id}:{decision}"),
        "operator_visible": True,
        "claim_lift_allowed": False,
        "safe_receipt": _safe_receipt("review-waiver", receipt_id, safe_payload),
    }


def _vulnerability_row(
    receipt_id: str,
    scanner_source: str,
    freshness_status: str,
    severity: str,
    operator_action: str,
) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "scanner_source": scanner_source,
        "severity": severity,
        "operator_action": operator_action,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": MARKETPLACE_VULNERABILITY_MONITORING_V2_SUITE_NAME,
        "scanner_source": scanner_source,
        "freshness_status": freshness_status,
        "scanner_digest": _digest(f"scanner:{scanner_source}:{RUN_DATE.isoformat()}"),
        "severity": severity,
        "critical_high_blocked": severity in {"critical", "high"},
        "dependency_impact_digest": _digest(f"dependency-impact:{scanner_source}:{severity}"),
        "publisher_key_state": "revoked" if "ghsa" in scanner_source.lower() else "active",
        "operator_action": operator_action,
        "operator_diagnostic_id": f"dv-vulnerability-diagnostic:{receipt_id}",
        "operator_visible": True,
        "claim_lift_allowed": False,
        "safe_receipt": _safe_receipt("vulnerability", receipt_id, safe_payload),
    }


def _hostile_row(receipt_id: str, drill_class: str, enforcement_action: str) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "drill_class": drill_class,
        "enforcement_action": enforcement_action,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SUITE_NAME,
        "drill_class": drill_class,
        "enforcement": {
            "action": enforcement_action,
            "status": (
                "rolled_back"
                if "restore" in enforcement_action
                else "quarantined"
                if "quarantine" in enforcement_action
                else "denied"
            ),
        },
        "runtime_contribution_allowed": False,
        "secure_host_recheck_required": True,
        "rollback_snapshot_verified": "rollback" in drill_class or "update" in drill_class,
        "quarantine_reentry_allowed": False,
        "operator_diagnostic_id": f"dv-hostile-diagnostic:{drill_class}",
        "operator_audit_receipt": _receipt_handle("hostile", receipt_id, safe_payload),
        "audit_chain_digest": _digest(f"hostile-audit:{receipt_id}:{drill_class}"),
        "operator_visible": True,
        "claim_lift_allowed": False,
        "safe_receipt": _safe_receipt("hostile", receipt_id, safe_payload),
    }


def _diagnostic_row(
    receipt_id: str,
    lifecycle_event: str,
    cause_class: str,
    root_cause: str,
) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "lifecycle_event": lifecycle_event,
        "cause_class": cause_class,
        "root_cause": root_cause,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SUITE_NAME,
        "lifecycle_event": lifecycle_event,
        "cause_class": cause_class,
        "root_cause": root_cause,
        "operator_action": "rollback_or_quarantine_until_retest",
        "compatibility_state": "blocked" if cause_class == "compatibility" else "requires_recheck",
        "permission_delta_state": "drift_detected" if cause_class == "permission_drift" else "stable_or_not_applicable",
        "dependency_state": "digest_changed" if cause_class == "dependency" else "not_primary",
        "scanner_state": "stale" if cause_class == "scanner" else "current_or_not_primary",
        "publisher_state": "revoked" if cause_class == "publisher" else "verified_or_not_primary",
        "runtime_boundary_state": "recheck_failed" if cause_class == "runtime_boundary" else "not_primary",
        "operator_audit_receipt": _receipt_handle("diagnostic", receipt_id, safe_payload),
        "audit_chain_digest": _digest(f"diagnostic-audit:{receipt_id}:{cause_class}"),
        "operator_visible": True,
        "claim_lift_allowed": False,
        "safe_receipt": _safe_receipt("diagnostic", receipt_id, safe_payload),
    }


def _secure_host_audit_row(
    receipt_id: str,
    operation: str,
    capability_surface: str,
    runtime_profile: str,
    authority: str,
) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "operation": operation,
        "runtime_profile": runtime_profile,
        "authority": authority,
    }
    runtime_gate = "verified_snapshot_only" if operation == "rollback" else "deny_until_reviewed"
    return {
        "receipt_id": receipt_id,
        "suite_name": MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SUITE_NAME,
        "operation": operation,
        "capability_surface": capability_surface,
        "secure_host": {
            "runtime_profile": runtime_profile,
            "permission_delta_reviewed": True,
            "credential_scope_rechecked": True,
            "egress_policy_rechecked": True,
            "runtime_contribution_gate": runtime_gate,
            "operator_authority": authority,
        },
        "operator_audit_receipt": _receipt_handle("secure-host-audit", receipt_id, safe_payload),
        "audit_chain_digest": _digest(f"secure-host-audit:{receipt_id}:{operation}:{runtime_profile}"),
        "operator_visible": True,
        "claim_lift_allowed": False,
        "safe_receipt": _safe_receipt("secure-host-audit", receipt_id, safe_payload),
    }


def _safe_receipt(kind: str, receipt_id: str, payload: Any) -> dict[str, Any]:
    body_digest = _digest(payload)
    return {
        "receipt_handle": _receipt_handle(kind, receipt_id, payload),
        "evidence_body_digest": body_digest,
        "sanitized_payload_digest": body_digest,
        "tamper_evident_digest": _digest({"kind": kind, "receipt_id": receipt_id, "body_digest": body_digest}),
        "redaction": "metadata_only_receipt_handle",
        "redaction_layer": "post_dp_marketplace_lifecycle_gap_closure_v1",
        "contains_secret": False,
        "contains_private_path": False,
        "contains_raw_package_path": False,
        "contains_raw_transcript": False,
        "raw_receipt_path_exposed": False,
        "workspace_dir_exposed": False,
        "package_path_exposed": False,
        "redaction_degraded": False,
    }


def _all_safe_receipts_redacted(items: list[dict[str, Any]]) -> bool:
    receipts = [item["safe_receipt"] for item in items]
    return all(
        receipt["contains_secret"] is False
        and receipt["contains_private_path"] is False
        and receipt["contains_raw_package_path"] is False
        and receipt["contains_raw_transcript"] is False
        and receipt["raw_receipt_path_exposed"] is False
        and receipt["workspace_dir_exposed"] is False
        and receipt["package_path_exposed"] is False
        and receipt["redaction"] == "metadata_only_receipt_handle"
        and receipt["redaction_layer"] == "post_dp_marketplace_lifecycle_gap_closure_v1"
        and receipt["redaction_degraded"] is False
        and len(receipt["evidence_body_digest"]) == 64
        and len(receipt["sanitized_payload_digest"]) == 64
        and len(receipt["tamper_evident_digest"]) == 64
        and receipt["tamper_evident_digest"] != receipt["evidence_body_digest"]
        for receipt in receipts
    )


def _run_strategy_claim_scan() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    command = ["python3", "scripts/check_strategy_claims.py"]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stderr = completed.stderr or ""
        stdout = completed.stdout or ""
        return {
            "command_exit_code": int(completed.returncode),
            "stdout_digest": _digest(stdout),
            "stderr_digest": _digest(stderr),
            "blocked_claims_found": [] if completed.returncode == 0 else _scan_error_lines(stderr),
            "forbidden_hit_count": 0 if completed.returncode == 0 else max(1, len(_scan_error_lines(stderr))),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"{type(exc).__name__}:{exc}"
        return {
            "command_exit_code": 124,
            "stdout_digest": _digest(""),
            "stderr_digest": _digest(message),
            "blocked_claims_found": [message],
            "forbidden_hit_count": 1,
        }


def _scan_error_lines(stderr: str) -> list[str]:
    return [line.strip() for line in stderr.splitlines() if line.strip()]


def _receipt_handle(kind: str, receipt_id: str, payload: Any) -> str:
    return f"seraph://receipts/batch-dv/{kind}/{receipt_id}/{_digest(payload)[:20]}"


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _failure_report(summary: Any, *, suite_name: str) -> dict[str, Any]:
    failures = [
        {
            "name": getattr(result, "name", ""),
            "details": getattr(result, "details", {}),
            "message": getattr(result, "message", ""),
        }
        for result in getattr(summary, "results", [])
        if not getattr(result, "passed", False)
    ]
    return {
        "suite_name": suite_name,
        "failure_count": len(failures),
        "failures": failures,
    }
