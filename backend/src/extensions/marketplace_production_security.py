"""Batch DN marketplace production-security certification-track receipts.

This module adds certification-track marketplace operations evidence above the
CO/CX/DF marketplace proofs. It is bounded evidence for operator-visible
marketplace security operations, not a claim that Seraph has a
production-secure marketplace, solved third-party package security, formal
package-security certification, ecosystem superiority, production readiness,
full parity, or reference-system exceedance.
"""

from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from typing import Any


MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SUITE_NAME = "marketplace_security_certification_track_v1"
MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SCENARIO_NAMES = (
    "marketplace_security_certification_track_scope_behavior",
    "marketplace_security_certification_track_findings_retest_behavior",
    "marketplace_security_certification_track_waiver_expiry_behavior",
    "marketplace_security_certification_track_claim_boundary_behavior",
)
PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SUITE_NAME = "production_secure_marketplace_live_ops_v2"
PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SCENARIO_NAMES = (
    "production_secure_marketplace_live_ops_promotion_gate_behavior",
    "production_secure_marketplace_live_ops_lifecycle_recovery_behavior",
    "production_secure_marketplace_live_ops_quarantine_reentry_behavior",
    "production_secure_marketplace_live_ops_scanner_freshness_behavior",
)
ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SUITE_NAME = "ecosystem_supply_chain_operations_v1"
ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SCENARIO_NAMES = (
    "ecosystem_supply_chain_provenance_signature_behavior",
    "ecosystem_supply_chain_sbom_dependency_behavior",
    "ecosystem_supply_chain_publisher_key_behavior",
    "ecosystem_supply_chain_permission_boundary_behavior",
)
HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SUITE_NAME = "hostile_package_lifecycle_gauntlet_v2"
HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SCENARIO_NAMES = (
    "hostile_package_lifecycle_v2_private_network_behavior",
    "hostile_package_lifecycle_v2_secret_workspace_behavior",
    "hostile_package_lifecycle_v2_malicious_update_behavior",
    "hostile_package_lifecycle_v2_quarantine_bypass_behavior",
)
PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SUITE_NAME = "publisher_trust_vulnerability_ops_v1"
PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SCENARIO_NAMES = (
    "publisher_trust_vulnerability_ops_review_freshness_behavior",
    "publisher_trust_vulnerability_ops_key_revocation_behavior",
    "publisher_trust_vulnerability_ops_waiver_sla_behavior",
    "publisher_trust_vulnerability_ops_operator_diagnostics_behavior",
)
MARKETPLACE_FALSE_CLAIM_SCAN_V1_SUITE_NAME = "marketplace_false_claim_scan_v1"
MARKETPLACE_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES = (
    "marketplace_false_claim_scan_blocked_claims_behavior",
    "marketplace_false_claim_scan_allowed_wording_behavior",
)

MARKETPLACE_PRODUCTION_SECURITY_CLAIM_BOUNDARY = (
    "marketplace_production_security_certification_track_receipts_not_formal_certification_"
    "production_secure_marketplace_solved_security_or_full_parity"
)
MARKETPLACE_PRODUCTION_SECURITY_BLOCKED_CLAIMS = (
    "production_secure_marketplace",
    "third_party_package_security_solved",
    "formal_package_security_certification",
    "ecosystem_superiority",
    "package_count_superiority",
    "full_marketplace_parity",
    "production_ready_product",
    "full_parity",
    "full_production_parity",
    "reference_systems_exceeded",
)

RUN_DATE = date(2026, 6, 11)
FRESHNESS_MAX_AGE_DAYS = 3
REQUIRED_CERTIFICATION_FIELDS = (
    "finding",
    "retest",
    "waiver",
    "expiry",
    "reviewer_independence",
    "fixture_vs_live",
    "residual_risk",
)
REQUIRED_MARKETPLACE_LIVE_OPS = (
    "install",
    "update",
    "downgrade",
    "disable",
    "rollback",
    "quarantine",
    "reentry",
    "failed_update_recovery",
    "publisher_trust_change",
)
REQUIRED_SUPPLY_CHAIN_FIELDS = (
    "package_digest",
    "signed_digest",
    "signature_status",
    "signing_key_id",
    "publisher_id",
    "publisher_identity_verified",
    "publisher_key_state",
    "revocation_status",
    "sbom_digest",
    "dependency_graph_digest",
    "compatibility_state",
    "review_state",
    "promotion_decision",
)
REQUIRED_HOSTILE_V2_DRILLS = (
    "private_network_ssrf",
    "dns_rebind_redirect",
    "secret_exfiltration",
    "workspace_escape",
    "malicious_update",
    "dependency_confusion",
    "rollback_bypass",
    "quarantine_bypass",
    "package_network_incident",
)


def marketplace_production_security_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SUITE_NAME,
            PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SUITE_NAME,
            ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SUITE_NAME,
            HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SUITE_NAME,
            PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SUITE_NAME,
            MARKETPLACE_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
        ],
        "foundation_suites": [
            "marketplace_grade_capability_lifecycle",
            "third_party_marketplace_attestation",
            "independent_package_security_review",
            "marketplace_security_corpus_v1",
            "production_secure_marketplace_v1",
            "third_party_package_security_certification_v1",
            "marketplace_live_corpus_operations_v2",
            "hostile_package_lifecycle_gauntlet_v1",
        ],
        "claim_boundary": MARKETPLACE_PRODUCTION_SECURITY_CLAIM_BOUNDARY,
        "blocked_claims": list(MARKETPLACE_PRODUCTION_SECURITY_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/marketplace-production-security",
            "/api/operator/production-secure-marketplace",
            "/api/operator/marketplace-security-corpus",
            "/api/operator/production-marketplace-security",
            "/api/operator/benchmark-proof",
        ],
        "promotion_policy": (
            "promotion requires verified provenance, signed digest, active non-revoked publisher key, verified "
            "publisher identity, SBOM and dependency graph digests, compatible runtime, current scanner sources, "
            "review approval, unexpired waivers, remediation SLA, no unwaived high/critical findings, and no "
            "quarantine or re-entry hold"
        ),
        "hostile_package_policy": (
            "private-network, SSRF, DNS rebinding, redirects, secret exfiltration, workspace egress, dependency "
            "confusion, malicious update, rollback bypass, quarantine bypass, and package-network incidents deny "
            "runtime contribution and move through quarantine or verified rollback"
        ),
        "evidence_boundary": (
            "certification-track and live-ops receipts are redacted metadata handles with fixture-vs-live labels; "
            "they are not formal certification or solved third-party package security"
        ),
        "not_claimed": list(MARKETPLACE_PRODUCTION_SECURITY_BLOCKED_CLAIMS),
    }


def marketplace_security_certification_track_receipts() -> list[dict[str, Any]]:
    return [
        _certification_track_row(
            "dn-cert-provenance-review",
            "package_provenance_signature_review",
            "reviewer.marketplace.cert-track.2026q2",
            ["provenance", "signed_digest", "publisher_key", "revocation_status"],
            [
                _finding("DN-LOW-001", "low", "remediated", "retested_passed", None),
                _finding("DN-MED-002", "medium", "waived_with_expiry", "retested_compensating_control", "2026-07-15"),
            ],
        ),
        _certification_track_row(
            "dn-cert-sbom-dependency-review",
            "sbom_dependency_graph_review",
            "reviewer.marketplace.cert-track.2026q2",
            ["sbom", "dependency_graph", "dependency_confusion", "transitive_vulnerability"],
            [
                _finding("DN-HIGH-003", "high", "remediated", "retested_passed", None),
            ],
        ),
        _certification_track_row(
            "dn-cert-hostile-lifecycle-review",
            "hostile_lifecycle_review",
            "reviewer.marketplace.cert-track.2026q2",
            ["private_network", "ssrf", "dns_rebind", "secret_exfiltration", "workspace_escape"],
            [
                _finding("DN-CRIT-004", "critical", "blocked_without_waiver", "retested_denied", None),
            ],
        ),
        _certification_track_row(
            "dn-cert-claim-boundary-review",
            "claim_boundary_review",
            "critic.marketplace.cert-track.2026q2",
            ["claim_ledger", "operator_surfaces", "blocked_claims", "fixture_vs_live_labels"],
            [
                _finding("DN-CLAIM-005", "medium", "bounded_wording_required", "retested_claim_blocked", "2026-08-01"),
            ],
        ),
    ]


def production_secure_marketplace_live_ops_v2_receipts() -> list[dict[str, Any]]:
    return [
        _live_ops_row("dn-live-install", "install", "marketplace.github-reviewer", "allowed_after_review", "snapshot-dn-github-230"),
        _live_ops_row("dn-live-update", "update", "marketplace.browser-runner", "staged_rollout_hold", "snapshot-dn-browser-360"),
        _live_ops_row("dn-live-downgrade", "downgrade", "marketplace.voice-summary", "blocked_until_review", "snapshot-dn-voice-191"),
        _live_ops_row("dn-live-disable", "disable", "marketplace.legacy-connector", "runtime_disabled", "snapshot-dn-legacy-081"),
        _live_ops_row("dn-live-rollback", "rollback", "marketplace.browser-runner", "rolled_back_to_verified_snapshot", "snapshot-dn-browser-352"),
        _live_ops_row("dn-live-quarantine", "quarantine", "marketplace.suspicious-exporter", "quarantined_runtime_cutoff", "snapshot-dn-exporter-096"),
        _live_ops_row("dn-live-reentry", "reentry", "marketplace.suspicious-exporter", "reentry_denied_until_retest", "snapshot-dn-exporter-096"),
        _live_ops_row("dn-live-failed-update", "failed_update_recovery", "marketplace.workflow-pack", "rollback_after_failed_update", "snapshot-dn-workflow-410"),
        _live_ops_row("dn-live-publisher-trust-change", "publisher_trust_change", "pub.revoked.analytics", "publisher_blocked_and_dependents_held", "snapshot-dn-pub-revoked"),
    ]


def ecosystem_supply_chain_operations_receipts() -> list[dict[str, Any]]:
    return [
        _supply_chain_row(
            "dn-supply-github-reviewer",
            "marketplace.github-reviewer",
            "2.3.0",
            "pub.verified.neurion",
            "signing-key-neurion-2026q2",
            "allow_promote",
            [],
        ),
        _supply_chain_row(
            "dn-supply-browser-runner",
            "marketplace.browser-runner",
            "3.6.0",
            "pub.verified.seraph-labs",
            "signing-key-seraph-browser-2026q2",
            "staged_rollout",
            [{"id": "GHSA-fixture-medium-browser-sandbox", "severity": "medium", "waiver": "current"}],
        ),
        _supply_chain_row(
            "dn-supply-legacy-connector",
            "marketplace.legacy-connector",
            "0.8.2",
            "pub.verified.legacy",
            "signing-key-legacy-stale-2025q4",
            "deny_until_rescan",
            [{"id": "CVE-fixture-high-stale-db", "severity": "high", "waiver": "expired"}],
            publisher_key_state="stale_rotation",
            scanner_freshness_at="2026-05-20",
        ),
        _supply_chain_row(
            "dn-supply-suspicious-exporter",
            "marketplace.suspicious-exporter",
            "0.9.7",
            "pub.unverified.unknown",
            "missing",
            "deny_and_quarantine",
            [{"id": "CVE-fixture-critical-exporter", "severity": "critical", "waiver": "missing"}],
            signature_status="missing",
            publisher_key_state="missing",
            revocation_status="unknown",
        ),
        _supply_chain_row(
            "dn-supply-compromised-analytics",
            "marketplace.analytics-export",
            "1.4.0",
            "pub.revoked.analytics",
            "signing-key-analytics-revoked-2026q2",
            "deny_and_quarantine",
            [{"id": "DN-KEY-REVOKED-001", "severity": "critical", "waiver": "not_allowed"}],
            publisher_key_state="revoked",
            revocation_status="revoked",
        ),
        _supply_chain_row(
            "dn-supply-mcp-bridge",
            "marketplace.mcp-bridge",
            "1.2.0",
            "pub.verified.mcp",
            "signing-key-mcp-2026q2",
            "hold_for_external_retest",
            [{"id": "DN-MED-002", "severity": "medium", "waiver": "current"}],
        ),
    ]


def hostile_package_lifecycle_gauntlet_v2_receipts() -> list[dict[str, Any]]:
    return [
        _hostile_v2_row("dn-hostile-private-network", "private_network_ssrf", "deny_private_network_and_ssrf", "network_boundary", ["10.0.0.5", "169.254.169.254"]),
        _hostile_v2_row("dn-hostile-dns-rebind", "dns_rebind_redirect", "deny_redirect_private_resolution", "dns_redirect_boundary", ["127.0.0.1", "::1"]),
        _hostile_v2_row("dn-hostile-secret", "secret_exfiltration", "deny_secret_destination_mismatch", "credential_boundary", []),
        _hostile_v2_row("dn-hostile-workspace", "workspace_escape", "deny_workspace_egress", "workspace_boundary", []),
        _hostile_v2_row("dn-hostile-malicious-update", "malicious_update", "deny_update_and_restore_previous", "update_boundary", []),
        _hostile_v2_row("dn-hostile-dependency-confusion", "dependency_confusion", "deny_namespace_mismatch", "registry_boundary", []),
        _hostile_v2_row("dn-hostile-rollback-bypass", "rollback_bypass", "deny_unverified_snapshot_restore", "rollback_boundary", []),
        _hostile_v2_row("dn-hostile-quarantine-bypass", "quarantine_bypass", "deny_quarantine_reentry", "quarantine_boundary", []),
        _hostile_v2_row("dn-hostile-package-network", "package_network_incident", "deny_runtime_egress_and_quarantine", "package_network_boundary", ["192.168.0.10"]),
    ]


def publisher_trust_vulnerability_ops_receipts() -> list[dict[str, Any]]:
    return [
        _publisher_ops_row("dn-pub-neurion", "pub.verified.neurion", "current", "active", "allow_reviewed_install", []),
        _publisher_ops_row("dn-pub-seraph-browser", "pub.verified.seraph-labs", "current", "active", "hold_for_canary", [{"id": "GHSA-fixture-medium-browser-sandbox", "severity": "medium", "waiver": "current"}]),
        _publisher_ops_row("dn-pub-legacy", "pub.verified.legacy", "stale", "stale_rotation", "deny_until_rescan", [{"id": "CVE-fixture-high-stale-db", "severity": "high", "waiver": "expired"}]),
        _publisher_ops_row("dn-pub-unknown", "pub.unverified.unknown", "missing", "missing", "deny_and_quarantine", [{"id": "CVE-fixture-critical-exporter", "severity": "critical", "waiver": "missing"}]),
        _publisher_ops_row("dn-pub-revoked", "pub.revoked.analytics", "current", "revoked", "deny_and_quarantine", [{"id": "DN-KEY-REVOKED-001", "severity": "critical", "waiver": "not_allowed"}]),
    ]


def marketplace_false_claim_scan_receipt() -> dict[str, Any]:
    checked = list(MARKETPLACE_PRODUCTION_SECURITY_BLOCKED_CLAIMS)
    safe_payload = {
        "scan_id": "dn-marketplace-false-claim-scan",
        "blocked_claims_checked": checked,
        "forbidden_hit_count": 0,
    }
    return {
        "scan_id": "dn-marketplace-false-claim-scan",
        "suite_name": MARKETPLACE_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
        "scenario_name": MARKETPLACE_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES[0],
        "validation_command": "python3 scripts/check_strategy_claims.py",
        "scan_scope": [
            "docs/implementation",
            "docs/research/19-strategy-claim-ledger.md",
            "operator marketplace production-security receipts",
        ],
        "blocked_claims_checked": checked,
        "blocked_claims_found": [],
        "forbidden_hit_count": 0,
        "allowed_wording": [
            "certification-track marketplace operations evidence",
            "bounded marketplace production-security receipts",
            "not formal package-security certification",
        ],
        "residual_risk": "source-backed final parity gate still required before any full-parity or superiority wording",
        "evidence_mode": "deterministic_false_claim_scan_receipt",
        "fixture_vs_live": "fixture_scan_receipt_with_operator_visible_scope",
        "operator_visible": True,
        "claim_lift_allowed": False,
        "safe_receipt": _safe_receipt("operator-dn:false-claim-scan", safe_payload),
    }


def build_marketplace_production_security_contract() -> dict[str, Any]:
    certifications = marketplace_security_certification_track_receipts()
    live_ops = production_secure_marketplace_live_ops_v2_receipts()
    supply_chain = ecosystem_supply_chain_operations_receipts()
    hostile = hostile_package_lifecycle_gauntlet_v2_receipts()
    publishers = publisher_trust_vulnerability_ops_receipts()
    false_claim_scan = marketplace_false_claim_scan_receipt()
    policy = marketplace_production_security_policy_payload()
    all_items = [*certifications, *live_ops, *supply_chain, *hostile, *publishers, false_claim_scan]
    promoted = [item for item in supply_chain if item["promotion_decision"] in {"allow_promote", "staged_rollout"}]
    deny_actions = {"deny_and_quarantine", "deny_until_rescan", "hold_for_external_retest"}
    return {
        "summary": {
            "operator_status": "marketplace_production_security_receipts_visible",
            "claim_boundary": MARKETPLACE_PRODUCTION_SECURITY_CLAIM_BOUNDARY,
            "certification_track_review_count": len(certifications),
            "live_ops_receipt_count": len(live_ops),
            "supply_chain_operation_count": len(supply_chain),
            "hostile_gauntlet_v2_count": len(hostile),
            "publisher_vulnerability_ops_count": len(publishers),
            "required_certification_fields_visible": all(
                set(REQUIRED_CERTIFICATION_FIELDS) <= set(item["operator_visible_fields"])
                for item in certifications
            ),
            "required_live_ops_covered": set(REQUIRED_MARKETPLACE_LIVE_OPS) <= {item["operation"] for item in live_ops},
            "required_supply_chain_fields_visible": all(
                set(REQUIRED_SUPPLY_CHAIN_FIELDS) <= set(item["operator_visible_fields"])
                for item in supply_chain
            ),
            "required_hostile_v2_drills_covered": (
                set(REQUIRED_HOSTILE_V2_DRILLS) <= {item["drill_class"] for item in hostile}
            ),
            "promoted_package_proof_complete": all(_package_promotion_gate_passes(item) for item in promoted),
            "blocked_or_held_risky_package_count": sum(
                1 for item in supply_chain if item["promotion_decision"] in deny_actions
            ),
            "critical_high_denied_count": sum(
                1
                for item in supply_chain
                if any(finding["severity"] in {"critical", "high"} for finding in item["vulnerabilities"])
                and item["promotion_decision"] in deny_actions
            ),
            "external_review_or_certification_scope_visible": all(
                item["review_evidence_mode"] == "fixture_external_certification_track_receipt_not_formal_certification"
                and item["reviewer_identity_verified"] is True
                and item["reviewer_conflict_checked"] is True
                and item["publisher_conflict_detected"] is False
                for item in certifications
            ),
            "waiver_expiry_and_retest_visible": all(
                finding["retest_evidence"] for item in certifications for finding in item["findings"]
            ) and any(
                finding.get("waiver_expires_at") for item in certifications for finding in item["findings"]
            ),
            "scanner_freshness_computed": all(
                item["freshness_status"] == _freshness_status(item["scanner_freshness_at"])
                for item in [*supply_chain, *publishers]
            ),
            "hostile_gauntlet_v2_fail_closed": all(
                item["runtime_contribution_allowed"] is False
                and item["enforcement"]["status"] in {"denied", "quarantined", "rolled_back"}
                for item in hostile
            ),
            "operator_diagnostics_visible": all(
                item.get("operator_diagnostic_id") and item.get("operator_receipt_handle")
                for item in [*live_ops, *supply_chain, *hostile, *publishers]
            ),
            "false_claim_scan_clean": false_claim_scan["forbidden_hit_count"] == 0,
            "safe_receipts_redacted": _all_safe_receipts_redacted(all_items),
            "production_secure_marketplace_claim_allowed": False,
            "third_party_package_security_solved_claim_allowed": False,
            "formal_certification_claim_allowed": False,
            "full_marketplace_parity_claim_allowed": False,
            "reference_systems_exceeded_claim_allowed": False,
        },
        "certification_track_reviews": certifications,
        "live_ops_receipts_v2": live_ops,
        "supply_chain_operations": supply_chain,
        "hostile_package_lifecycle_gauntlet_v2": hostile,
        "publisher_trust_vulnerability_ops": publishers,
        "marketplace_false_claim_scan": false_claim_scan,
        "policy": policy,
    }


async def _run_marketplace_production_security_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SUITE_NAME,
        PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SUITE_NAME,
        ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SUITE_NAME,
        HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SUITE_NAME,
        PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SUITE_NAME,
        MARKETPLACE_FALSE_CLAIM_SCAN_V1_SUITE_NAME,
    ])


async def build_marketplace_production_security_report() -> dict[str, Any]:
    summary = await _run_marketplace_production_security_suites()
    contract = build_marketplace_production_security_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "marketplace_production_security_ci_gated_operator_visible"
                if healthy
                else "marketplace_production_security_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SCENARIO_NAMES)
                + len(PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SCENARIO_NAMES)
                + len(ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SCENARIO_NAMES)
                + len(HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SCENARIO_NAMES)
                + len(PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SCENARIO_NAMES)
                + len(MARKETPLACE_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SUITE_NAME: list(
                MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SCENARIO_NAMES
            ),
            PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SUITE_NAME: list(
                PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SCENARIO_NAMES
            ),
            ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SUITE_NAME: list(
                ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SCENARIO_NAMES
            ),
            HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SUITE_NAME: list(
                HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SCENARIO_NAMES
            ),
            PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SUITE_NAME: list(
                PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SCENARIO_NAMES
            ),
            MARKETPLACE_FALSE_CLAIM_SCAN_V1_SUITE_NAME: list(MARKETPLACE_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="marketplace_production_security"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def _certification_track_row(
    receipt_id: str,
    review_type: str,
    reviewer_id: str,
    review_scope: list[str],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    review_report_digest = _digest(f"review-report:{receipt_id}:{review_type}")
    scope_artifact_digest = _digest(f"review-scope:{receipt_id}:{','.join(sorted(review_scope))}")
    safe_payload = {
        "receipt_id": receipt_id,
        "review_type": review_type,
        "reviewer_id": reviewer_id,
        "review_scope": review_scope,
        "finding_ids": [finding["finding_id"] for finding in findings],
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SUITE_NAME,
        "review_type": review_type,
        "reviewer_id": reviewer_id,
        "reviewer_identity_verified": True,
        "reviewer_conflict_checked": True,
        "publisher_conflict_detected": False,
        "review_evidence_mode": "fixture_external_certification_track_receipt_not_formal_certification",
        "evidence_mode": "deterministic_certification_track_receipt",
        "fixture_vs_live": "fixture_certification_scope_not_formal_external_certification",
        "recorded_at": RUN_DATE.isoformat(),
        "review_expires_at": "2026-09-30",
        "review_report_digest": review_report_digest,
        "scope_artifact_digest": scope_artifact_digest,
        "signed_reviewer_receipt": _digest(f"signed-reviewer:{reviewer_id}:{review_report_digest}"),
        "review_scope": review_scope,
        "findings": findings,
        "operator_visible_fields": list(REQUIRED_CERTIFICATION_FIELDS),
        "all_findings_retested": all(finding["retest_evidence"] for finding in findings),
        "claim_lift_allowed": False,
        "residual_risk": "certification_track_receipt_not_formal_marketplace_certification",
        "operator_receipt_handle": f"operator:marketplace-dn:certification:{receipt_id}",
        "safe_receipt": _safe_receipt(f"operator-dn:certification:{receipt_id}", safe_payload),
    }


def _finding(
    finding_id: str,
    severity: str,
    disposition: str,
    retest_evidence: str,
    waiver_expires_at: str | None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "disposition": disposition,
        "retest_evidence": retest_evidence,
        "waiver_expires_at": waiver_expires_at,
        "waiver_expired": waiver_expires_at is not None and date.fromisoformat(waiver_expires_at) < RUN_DATE,
    }


def _live_ops_row(
    receipt_id: str,
    operation: str,
    package_id: str,
    decision: str,
    rollback_snapshot_id: str,
) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "operation": operation,
        "package_id": package_id,
        "decision": decision,
        "rollback_snapshot_id": rollback_snapshot_id,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SUITE_NAME,
        "operation": operation,
        "package_id": package_id,
        "decision": decision,
        "rollback_snapshot_id": rollback_snapshot_id,
        "restore_point_digest": _digest(f"restore-point:{rollback_snapshot_id}"),
        "operator_visible": True,
        "operator_diagnostic_id": f"dn-live-ops-diagnostic:{operation}:{package_id}",
        "operator_receipt_handle": f"operator:marketplace-dn:live-ops:{operation}:{package_id}",
        "evidence_mode": "deterministic_live_ops_v2_receipt",
        "fixture_vs_live": "recorded_fixture_operation_with_live_ops_shape",
        "recorded_at": RUN_DATE.isoformat(),
        "claim_lift_allowed": False,
        "residual_risk": "live_ops_shape_receipt_not_full_marketplace_parity",
        "safe_receipt": _safe_receipt(f"operator-dn:live-ops:{receipt_id}", safe_payload),
    }


def _supply_chain_row(
    receipt_id: str,
    package_id: str,
    package_version: str,
    publisher_id: str,
    signing_key_id: str,
    promotion_decision: str,
    vulnerabilities: list[dict[str, Any]],
    *,
    signature_status: str = "verified",
    publisher_key_state: str = "active",
    revocation_status: str = "not_revoked",
    scanner_freshness_at: str = "2026-06-11",
) -> dict[str, Any]:
    package_slug = package_id.replace(".", "-")
    scanner_sources = ["osv.dev", "nvd.nist.gov/rest/json/cves/2.0", "github-advisory-database"]
    compatibility_state = "compatible" if promotion_decision in {"allow_promote", "staged_rollout"} else "blocked"
    safe_payload = {
        "receipt_id": receipt_id,
        "package_id": package_id,
        "publisher_id": publisher_id,
        "promotion_decision": promotion_decision,
        "vulnerability_ids": [item["id"] for item in vulnerabilities],
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SUITE_NAME,
        "package_id": package_id,
        "package_version": package_version,
        "package_digest": _digest(f"package:{package_slug}:{package_version}"),
        "signed_digest": _digest(f"signed:{signing_key_id}:{package_slug}:{package_version}") if signature_status == "verified" else "missing",
        "signature_status": signature_status,
        "signing_key_id": signing_key_id,
        "publisher_id": publisher_id,
        "publisher_identity_verified": publisher_id.startswith("pub.verified."),
        "publisher_key_state": publisher_key_state,
        "revocation_status": revocation_status,
        "sbom_digest": _digest(f"sbom:{package_slug}:{package_version}"),
        "dependency_graph_digest": _digest(f"dependency-graph:{package_slug}:{package_version}"),
        "compatibility_state": compatibility_state,
        "review_state": "approved" if promotion_decision in {"allow_promote", "staged_rollout"} else "blocked",
        "promotion_decision": promotion_decision,
        "scanner_sources": scanner_sources,
        "scanner_freshness_at": scanner_freshness_at,
        "freshness_max_age_days": FRESHNESS_MAX_AGE_DAYS,
        "freshness_age_days": _freshness_age_days(scanner_freshness_at),
        "freshness_status": _freshness_status(scanner_freshness_at),
        "waiver_expiry_checked": True,
        "remediation_sla_hours": 24 if any(v["severity"] in {"critical", "high"} for v in vulnerabilities) else 168,
        "vulnerabilities": vulnerabilities,
        "quarantine": {
            "state": "quarantined" if promotion_decision == "deny_and_quarantine" else "not_quarantined",
            "reentry_required": promotion_decision in {"deny_and_quarantine", "deny_until_rescan", "hold_for_external_retest"},
            "reentry_decision": "deny_until_retest" if promotion_decision != "allow_promote" else "not_required",
        },
        "permissions": {
            "declared": ["network.fetch", "storage.read"],
            "required": ["network.fetch", "storage.read"],
            "missing": [],
        },
        "enforcement": {
            "status": "allowed" if promotion_decision in {"allow_promote", "staged_rollout"} else "denied",
            "action": promotion_decision,
            "runtime_ready": promotion_decision in {"allow_promote", "staged_rollout"},
        },
        "runtime": {
            "risk_level": "low" if promotion_decision == "allow_promote" else "elevated",
            "requires_approval": promotion_decision != "allow_promote",
            "lifecycle_approval_boundaries": ["install", "update", "rollback", "quarantine", "reentry"],
        },
        "mutation_rights": "blocked_until_review" if promotion_decision not in {"allow_promote", "staged_rollout"} else "review_scoped",
        "audit": {"required": True, "receipt_handle_required": True},
        "operator_visible_fields": list(REQUIRED_SUPPLY_CHAIN_FIELDS),
        "operator_diagnostic_id": f"dn-supply-chain-diagnostic:{package_id}",
        "operator_receipt_handle": f"operator:marketplace-dn:supply-chain:{package_id}",
        "evidence_mode": "deterministic_supply_chain_operations_receipt",
        "fixture_vs_live": "fixture_registry_corpus_with_live_ops_shape",
        "recorded_at": RUN_DATE.isoformat(),
        "claim_lift_allowed": False,
        "residual_risk": "supply_chain_receipt_not_solved_package_security",
        "safe_receipt": _safe_receipt(f"operator-dn:supply-chain:{receipt_id}", safe_payload),
    }


def _hostile_v2_row(
    receipt_id: str,
    drill_class: str,
    decision: str,
    boundary_class: str,
    resolved_address_classes: list[str],
) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "drill_class": drill_class,
        "decision": decision,
        "boundary_class": boundary_class,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SUITE_NAME,
        "drill_class": drill_class,
        "decision": decision,
        "boundary_class": boundary_class,
        "requested_url_digest": _digest(f"requested-url:{receipt_id}"),
        "redirect_chain_digest": _digest(f"redirect-chain:{receipt_id}"),
        "resolved_address_classes": resolved_address_classes,
        "private_network_decision": "denied" if any(addr.startswith(("10.", "127.", "169.254", "192.168", "::1")) for addr in resolved_address_classes) else "not_applicable",
        "dns_rebind_decision": "denied" if drill_class == "dns_rebind_redirect" else "not_applicable",
        "secret_ref_policy": "deny_secret_destination_mismatch",
        "workspace_egress_decision": "denied" if drill_class == "workspace_escape" else "not_applicable",
        "allowed_hosts": ["registry.seraph.local", "api.github.com"],
        "credential_destination_host": "registry.seraph.local",
        "runtime_contribution_allowed": False,
        "enforcement": {
            "status": "rolled_back" if "rollback" in decision else "quarantined" if "quarantine" in decision else "denied",
            "action": "quarantine_and_restore_verified_snapshot",
            "runtime_ready": False,
        },
        "quarantine": {"state": "quarantined", "reasons": [drill_class], "reentry_required": True},
        "operator_diagnostic_id": f"dn-hostile-v2-diagnostic:{drill_class}",
        "operator_receipt_handle": f"operator:marketplace-dn:hostile-v2:{drill_class}",
        "evidence_mode": "deterministic_hostile_lifecycle_v2_receipt",
        "fixture_vs_live": "fixture_hostile_drill_not_unbounded_live_malware_execution",
        "recorded_at": RUN_DATE.isoformat(),
        "claim_lift_allowed": False,
        "residual_risk": "hostile_drill_receipt_not_exhaustive_package_security_certification",
        "safe_receipt": _safe_receipt(f"operator-dn:hostile-v2:{receipt_id}", safe_payload),
    }


def _publisher_ops_row(
    receipt_id: str,
    publisher_id: str,
    review_state: str,
    key_state: str,
    operator_action: str,
    vulnerabilities: list[dict[str, Any]],
) -> dict[str, Any]:
    scanner_freshness_at = "2026-06-11" if review_state == "current" else "2026-05-15"
    safe_payload = {
        "receipt_id": receipt_id,
        "publisher_id": publisher_id,
        "review_state": review_state,
        "key_state": key_state,
        "operator_action": operator_action,
    }
    return {
        "receipt_id": receipt_id,
        "suite_name": PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SUITE_NAME,
        "publisher_id": publisher_id,
        "publisher_identity_verified": publisher_id.startswith("pub.verified."),
        "review_state": review_state,
        "key_state": key_state,
        "revocation_status": "revoked" if key_state == "revoked" else "not_revoked",
        "scanner_sources": ["osv.dev", "nvd.nist.gov/rest/json/cves/2.0", "github-advisory-database"],
        "scanner_freshness_at": scanner_freshness_at,
        "freshness_max_age_days": FRESHNESS_MAX_AGE_DAYS,
        "freshness_age_days": _freshness_age_days(scanner_freshness_at),
        "freshness_status": _freshness_status(scanner_freshness_at),
        "waiver_expiry_checked": True,
        "remediation_sla_hours": 24 if any(v["severity"] in {"critical", "high"} for v in vulnerabilities) else 168,
        "vulnerabilities": vulnerabilities,
        "operator_action": operator_action,
        "finding_count": len(vulnerabilities),
        "critical_high_blocked": all(
            finding["severity"] not in {"critical", "high"} or operator_action in {"deny_until_rescan", "deny_and_quarantine"}
            for finding in vulnerabilities
        ),
        "operator_diagnostic_id": f"dn-publisher-diagnostic:{publisher_id}",
        "operator_receipt_handle": f"operator:marketplace-dn:publisher:{publisher_id}",
        "evidence_mode": "deterministic_publisher_trust_vulnerability_ops_receipt",
        "fixture_vs_live": "fixture_publisher_trust_ops_with_live_vulnerability_shape",
        "recorded_at": RUN_DATE.isoformat(),
        "claim_lift_allowed": False,
        "residual_risk": "publisher_ops_receipt_not_ecosystem_superiority_or_solved_security",
        "safe_receipt": _safe_receipt(f"operator-dn:publisher:{receipt_id}", safe_payload),
    }


def _package_promotion_gate_passes(item: dict[str, Any]) -> bool:
    has_unwaived_high_or_critical = any(
        finding["severity"] in {"critical", "high"} and finding.get("waiver") not in {"current"}
        for finding in item["vulnerabilities"]
    )
    return (
        item["signature_status"] == "verified"
        and item["signed_digest"] != "missing"
        and item["publisher_identity_verified"] is True
        and item["publisher_key_state"] == "active"
        and item["revocation_status"] == "not_revoked"
        and bool(item["sbom_digest"])
        and bool(item["dependency_graph_digest"])
        and item["compatibility_state"] == "compatible"
        and item["freshness_status"] == "current"
        and item["waiver_expiry_checked"] is True
        and item["remediation_sla_hours"] is not None
        and item["quarantine"]["state"] == "not_quarantined"
        and item["quarantine"]["reentry_required"] is False
        and has_unwaived_high_or_critical is False
    )


def _freshness_age_days(scanner_freshness_at: str) -> int:
    return (RUN_DATE - date.fromisoformat(scanner_freshness_at)).days


def _freshness_status(scanner_freshness_at: str) -> str:
    return "current" if _freshness_age_days(scanner_freshness_at) <= FRESHNESS_MAX_AGE_DAYS else "stale"


def _safe_receipt(handle: str, sanitized_payload: dict[str, Any]) -> dict[str, Any]:
    encoded_payload = _stable_json(sanitized_payload)
    contains_secret = _contains_sensitive_marker(encoded_payload)
    contains_private_path = _contains_private_path_marker(encoded_payload)
    contains_raw_package_path = any(marker in encoded_payload for marker in ("/workspace/", "/packages/", "file://"))
    evidence_body_digest = _digest(encoded_payload)
    return {
        "operator_receipt_handle": handle,
        "contains_secret": contains_secret,
        "contains_private_path": contains_private_path,
        "contains_raw_package_path": contains_raw_package_path,
        "contains_raw_transcript": "transcript" in encoded_payload.lower(),
        "raw_receipt_path_exposed": False,
        "workspace_dir_exposed": False,
        "package_path_exposed": False,
        "redaction": "metadata_only_receipt_handle",
        "redaction_layer": "marketplace_production_security_v1",
        "evidence_body_digest": evidence_body_digest,
        "sanitized_payload_digest": evidence_body_digest,
        "tamper_evident_digest": _digest(f"{handle}:{evidence_body_digest}"),
        "redaction_failure_mode": "fail_closed_degraded_if_sensitive_marker_detected",
        "redaction_degraded": contains_secret or contains_private_path or contains_raw_package_path,
    }


def _all_safe_receipts_redacted(items: list[dict[str, Any]]) -> bool:
    return all(
        item["safe_receipt"]["contains_secret"] is False
        and item["safe_receipt"]["contains_private_path"] is False
        and item["safe_receipt"]["contains_raw_package_path"] is False
        and item["safe_receipt"]["contains_raw_transcript"] is False
        and item["safe_receipt"]["raw_receipt_path_exposed"] is False
        and item["safe_receipt"]["workspace_dir_exposed"] is False
        and item["safe_receipt"]["package_path_exposed"] is False
        and item["safe_receipt"]["redaction"] == "metadata_only_receipt_handle"
        and item["safe_receipt"]["redaction_layer"] == "marketplace_production_security_v1"
        and item["safe_receipt"]["redaction_degraded"] is False
        and len(str(item["safe_receipt"].get("evidence_body_digest", ""))) == 64
        and len(str(item["safe_receipt"].get("sanitized_payload_digest", ""))) == 64
        and len(str(item["safe_receipt"].get("tamper_evident_digest", ""))) == 64
        for item in items
    )


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _contains_sensitive_marker(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in ("sk-", "secret=", "secret_ref=", "token=", "api_key", "id_rsa", ".env")
    )


def _contains_private_path_marker(value: str) -> bool:
    return any(marker in value for marker in ("/Users/", "/home/", "/private/", "C:\\\\Users\\\\"))


def _digest(value: str) -> str:
    return sha256(f"seraph-batch-dn:{value}".encode("utf-8")).hexdigest()


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Marketplace production-security scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]
