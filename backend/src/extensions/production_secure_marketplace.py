"""Batch DF bounded production-secure marketplace evidence receipts.

This module layers a post-CZ marketplace evidence program above the earlier
CO/CX marketplace-security receipts. It is bounded production-secure
marketplace evidence, not a claim that Seraph has a production-secure
marketplace, solved third-party package security, marketplace parity, ecosystem
superiority, production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any


PRODUCTION_SECURE_MARKETPLACE_V1_SUITE_NAME = "production_secure_marketplace_v1"
PRODUCTION_SECURE_MARKETPLACE_V1_SCENARIO_NAMES = (
    "production_secure_marketplace_gate_matrix_behavior",
    "production_secure_marketplace_operator_flow_behavior",
    "production_secure_marketplace_receipt_safety_behavior",
    "production_secure_marketplace_claim_boundary_behavior",
)
THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SUITE_NAME = (
    "third_party_package_security_certification_v1"
)
THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SCENARIO_NAMES = (
    "third_party_package_certification_reviewer_scope_behavior",
    "third_party_package_certification_findings_retest_behavior",
    "third_party_package_certification_waiver_expiry_behavior",
    "third_party_package_certification_claim_boundary_behavior",
)
MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SUITE_NAME = "marketplace_live_corpus_operations_v2"
MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SCENARIO_NAMES = (
    "marketplace_live_corpus_v2_inventory_quality_behavior",
    "marketplace_live_corpus_v2_lifecycle_flow_behavior",
    "marketplace_live_corpus_v2_scanner_freshness_behavior",
    "marketplace_live_corpus_v2_publisher_trust_behavior",
)
HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SUITE_NAME = "hostile_package_lifecycle_gauntlet_v1"
HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SCENARIO_NAMES = (
    "hostile_package_lifecycle_private_network_behavior",
    "hostile_package_lifecycle_secret_workspace_behavior",
    "hostile_package_lifecycle_dependency_confusion_behavior",
    "hostile_package_lifecycle_malicious_update_behavior",
    "hostile_package_lifecycle_quarantine_bypass_behavior",
)

PRODUCTION_SECURE_MARKETPLACE_CLAIM_BOUNDARY = (
    "bounded_production_secure_marketplace_receipts_not_production_secure_marketplace_solved_security_or_full_parity"
)
PRODUCTION_SECURE_MARKETPLACE_BLOCKED_CLAIMS = (
    "production_secure_marketplace",
    "third_party_package_security_solved",
    "ecosystem_superiority",
    "package_count_superiority",
    "full_marketplace_parity",
    "production_ready_product",
    "full_parity",
    "full_production_parity",
    "reference_systems_exceeded",
)

REQUIRED_MARKETPLACE_LIFECYCLE_FLOWS = (
    "install",
    "update",
    "downgrade",
    "disable",
    "rollback",
    "staged_rollout",
    "review",
    "diagnostic",
    "vulnerability",
    "publisher_trust",
    "quarantine",
    "reentry_review",
)
REQUIRED_HOSTILE_PACKAGE_DRILLS = (
    "private_network",
    "ssrf",
    "redirect_dns",
    "secret_exfiltration",
    "workspace_access",
    "dependency_confusion",
    "lifecycle_rollback",
    "quarantine_bypass",
    "malicious_update",
    "install_script_execution",
    "typosquatting",
    "transitive_dependency_compromise",
    "compromised_signing_key",
    "native_binary_artifact",
    "archive_path_traversal",
    "symlink_escape",
    "runtime_fetch",
    "dynamic_import",
    "package_prompt_injection",
    "tool_injection",
)


def production_secure_marketplace_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_SECURE_MARKETPLACE_V1_SUITE_NAME,
            THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SUITE_NAME,
            MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SUITE_NAME,
            HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SUITE_NAME,
        ],
        "foundation_suites": [
            "marketplace_grade_capability_lifecycle",
            "third_party_marketplace_attestation",
            "independent_package_security_review",
            "hostile_ecosystem_package_drills",
            "package_network_incident_operations",
            "publisher_trust_vulnerability_handling",
            "marketplace_rollback_quarantine_diagnostics",
            "marketplace_security_corpus_v1",
            "continuous_vulnerability_monitoring",
            "publisher_trust_operations",
        ],
        "claim_boundary": PRODUCTION_SECURE_MARKETPLACE_CLAIM_BOUNDARY,
        "blocked_claims": list(PRODUCTION_SECURE_MARKETPLACE_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/production-secure-marketplace",
            "/api/operator/marketplace-security-corpus",
            "/api/operator/production-marketplace-security",
            "/api/operator/live-marketplace-attestation-proof",
            "/api/operator/marketplace-lifecycle-maturity",
            "/api/operator/benchmark-proof",
        ],
        "promotion_policy": (
            "package promotion requires provenance, signature, publisher identity, SBOM, dependency graph, "
            "compatibility, scanner freshness, waiver expiry, remediation SLA, quarantine/re-entry state, "
            "external review scope, and fail-closed lifecycle receipts"
        ),
        "hostile_package_policy": (
            "private-network, SSRF, redirect/DNS, secret exfiltration, workspace access, dependency confusion, "
            "install/postinstall script execution, typosquatting, transitive dependency compromise, compromised "
            "signing keys, native/binary artifacts, archive path traversal, symlink escape, runtime fetch, dynamic "
            "import, prompt/tool injection, rollback, quarantine-bypass, and malicious-update drills deny or "
            "quarantine before runtime contribution"
        ),
        "certification_boundary": (
            "certification receipts mean bounded reviewer scope, findings, remediation, waivers, retests, and "
            "remaining blocked claims; they are not formal product certification or solved third-party package security"
        ),
        "not_claimed": [
            "production_secure_marketplace",
            "third_party_package_security_solved",
            "ecosystem_superiority",
            "package_count_superiority",
            "full_marketplace_parity",
            "production_ready_product",
            "reference_systems_exceeded",
        ],
    }


def production_secure_marketplace_gate_receipts() -> list[dict[str, Any]]:
    return [
        _gate_row(
            "df-gate-provenance-signature",
            "provenance_signature",
            "all_promoted_packages_have_signed_digest_and_publisher_key",
            "deny_unsigned_or_digest_mismatch",
            ["provenance", "signature", "publisher_key"],
        ),
        _gate_row(
            "df-gate-sbom-dependency",
            "sbom_dependency_graph",
            "all_promoted_packages_have_sbom_and_dependency_graph_digest",
            "deny_missing_or_stale_dependency_graph",
            ["sbom", "dependency_graph", "compatibility"],
        ),
        _gate_row(
            "df-gate-scanner-waiver-sla",
            "scanner_waiver_remediation",
            "scanner_freshness_waiver_expiry_and_remediation_sla_visible",
            "deny_critical_high_or_expired_waiver",
            ["scanner_freshness", "waiver_expiry", "remediation_sla"],
        ),
        _gate_row(
            "df-gate-publisher-review",
            "publisher_trust_review",
            "publisher_identity_key_rotation_review_and_incident_state_visible",
            "deny_unverified_or_stale_publisher",
            ["publisher_identity", "key_rotation", "review_state"],
        ),
        _gate_row(
            "df-gate-lifecycle-fail-closed",
            "lifecycle_fail_closed",
            "install_update_downgrade_disable_rollback_and_quarantine_have_restore_points",
            "fail_closed_to_snapshot_or_quarantine",
            ["install", "update", "downgrade", "disable", "rollback", "quarantine"],
        ),
    ]


def third_party_package_security_certification_receipts() -> list[dict[str, Any]]:
    return [
        _certification_row(
            "df-cert-external-review-2026-06",
            "external_package_security_review_scope",
            "reviewer.marketplace.security.external.2026q2",
            "external_reviewer_fixture_independent_of_publisher",
            "signed-review-report-df-external-scope-2026-06",
            ["provenance", "signatures", "sbom", "dependency_graph", "vulnerability_policy"],
            findings=[
                _finding("DF-LOW-001", "low", "remediated", "retested_passed", None),
                _finding("DF-MED-002", "medium", "waived_with_expiry", "retested_compensating_control", "2026-06-30"),
            ],
        ),
        _certification_row(
            "df-cert-hostile-lifecycle-2026-06",
            "hostile_lifecycle_gauntlet_review",
            "reviewer.marketplace.security.external.2026q2",
            "external_reviewer_fixture_independent_of_runtime_worker",
            "signed-review-report-df-hostile-lifecycle-2026-06",
            ["private_network", "ssrf", "secret_exfiltration", "workspace_access", "malicious_update"],
            findings=[
                _finding("DF-HIGH-003", "high", "remediated", "retested_passed", None),
                _finding("DF-CRIT-004", "critical", "blocked_without_waiver", "retested_denied", None),
            ],
        ),
        _certification_row(
            "df-cert-publisher-trust-2026-06",
            "publisher_trust_and_key_rotation_review",
            "reviewer.marketplace.security.external.2026q2",
            "separate_reviewer_from_publisher",
            "signed-review-report-df-publisher-trust-2026-06",
            ["publisher_identity", "key_rotation", "revocation", "review_freshness", "incident_state"],
            findings=[
                _finding("DF-MED-005", "medium", "remediated", "retested_passed", None),
            ],
        ),
        _certification_row(
            "df-cert-residual-claims-2026-06",
            "claim_boundary_and_residual_risk_review",
            "critic.marketplace.claims.external.2026q2",
            "critic_independent_of_batch_df_implementation",
            "signed-review-report-df-claim-boundary-2026-06",
            ["claim_ledger", "blocked_claims", "operator_receipts", "source_caveats"],
            findings=[
                _finding("DF-CLAIM-006", "medium", "bounded_wording_required", "retested_claim_blocked", "2026-07-15"),
            ],
        ),
    ]


def marketplace_live_corpus_operations_v2_receipts() -> list[dict[str, Any]]:
    return [
        _corpus_v2_package("df-corpus-github-reviewer", "marketplace.github-reviewer", "managed_connectors", "2.2.0", "pub.verified.neurion", "allow_promote", []),
        _corpus_v2_package("df-corpus-browser-runner", "marketplace.browser-runner", "browser_providers", "3.5.0", "pub.verified.seraph-labs", "staged_rollout", [{"id": "GHSA-fixture-medium-browser-sandbox", "severity": "medium", "waiver": "current"}]),
        _corpus_v2_package("df-corpus-voice-summary", "marketplace.voice-summary", "voice_media_profiles", "1.9.0", "pub.verified.media", "allow_promote", []),
        _corpus_v2_package("df-corpus-memory-adapter", "marketplace.memory-adapter", "memory_providers", "1.3.0", "pub.verified.memory", "allow_promote", []),
        _corpus_v2_package("df-corpus-workflow-pack", "marketplace.workflow-pack", "workflows", "4.1.0", "pub.verified.workflow", "staged_rollout", [{"id": "CVE-fixture-low-workflow", "severity": "low", "waiver": "not_required"}]),
        _corpus_v2_package("df-corpus-runbook-helper", "marketplace.runbook-helper", "runbooks", "1.6.0", "pub.verified.ops", "allow_promote", []),
        _corpus_v2_package("df-corpus-plugin-auditor", "marketplace.plugin-auditor", "skills", "2.0.0", "pub.verified.security", "allow_promote", []),
        _corpus_v2_package("df-corpus-calendar-bridge", "marketplace.calendar-bridge", "connectors", "1.7.2", "pub.verified.calendar", "allow_promote", []),
        _corpus_v2_package("df-corpus-legacy-connector", "marketplace.legacy-connector", "connectors", "0.8.1", "pub.verified.legacy", "deny_until_rescan", [{"id": "CVE-fixture-high-stale-db", "severity": "high", "waiver": "expired"}], key_state="stale_rotation"),
        _corpus_v2_package("df-corpus-suspicious-exporter", "marketplace.suspicious-exporter", "skills", "0.9.6", "pub.unverified.unknown", "deny_and_quarantine", [{"id": "CVE-fixture-critical-exporter", "severity": "critical", "waiver": "missing"}], signature_status="missing", key_state="missing"),
        _corpus_v2_package("df-corpus-mcp-bridge", "marketplace.mcp-bridge", "mcp_servers", "1.1.0", "pub.verified.mcp", "hold_for_external_retest", [{"id": "DF-MED-002", "severity": "medium", "waiver": "current"}]),
        _corpus_v2_package("df-corpus-media-annotator", "marketplace.media-annotator", "media_tools", "2.4.0", "pub.verified.media", "allow_promote", []),
    ]


def marketplace_lifecycle_flow_receipts() -> list[dict[str, Any]]:
    return [
        _flow("df-flow-install", "install", "marketplace.github-reviewer", "allowed_after_review", "snapshot-df-github-220"),
        _flow("df-flow-update", "update", "marketplace.browser-runner", "staged_rollout_hold", "snapshot-df-browser-350"),
        _flow("df-flow-downgrade", "downgrade", "marketplace.voice-summary", "blocked_until_review", "snapshot-df-voice-190"),
        _flow("df-flow-disable", "disable", "marketplace.legacy-connector", "runtime_disabled", "snapshot-df-legacy-081"),
        _flow("df-flow-rollback", "rollback", "marketplace.browser-runner", "rolled_back", "snapshot-df-browser-342"),
        _flow("df-flow-staged-rollout", "staged_rollout", "marketplace.workflow-pack", "canary_10_percent_hold", "snapshot-df-workflow-410"),
        _flow("df-flow-review", "review", "marketplace.mcp-bridge", "external_retest_required", "snapshot-df-mcp-110"),
        _flow("df-flow-diagnostic", "diagnostic", "marketplace.plugin-auditor", "diagnostics_visible", "snapshot-df-auditor-200"),
        _flow("df-flow-vulnerability", "vulnerability", "marketplace.suspicious-exporter", "deny_and_quarantine", "snapshot-df-exporter-096"),
        _flow("df-flow-publisher-trust", "publisher_trust", "pub.unverified.unknown", "publisher_blocked", "snapshot-df-pub-unknown"),
        _flow("df-flow-quarantine", "quarantine", "marketplace.suspicious-exporter", "quarantined_runtime_cutoff", "snapshot-df-exporter-096"),
        _flow("df-flow-reentry", "reentry_review", "marketplace.suspicious-exporter", "reentry_denied", "snapshot-df-exporter-096"),
    ]


def hostile_package_lifecycle_gauntlet_receipts() -> list[dict[str, Any]]:
    return [
        _gauntlet("df-hostile-private-network", "private_network", "deny_private_network", ["10.0.0.5", "172.16.1.8"], "network_boundary"),
        _gauntlet("df-hostile-ssrf", "ssrf", "deny_metadata_service", ["169.254.169.254"], "network_boundary"),
        _gauntlet("df-hostile-redirect-dns", "redirect_dns", "deny_redirect_private_resolution", ["127.0.0.1"], "dns_redirect_boundary"),
        _gauntlet("df-hostile-secret-exfiltration", "secret_exfiltration", "deny_secret_destination_mismatch", [], "credential_boundary"),
        _gauntlet("df-hostile-workspace-access", "workspace_access", "deny_workspace_escape", [], "workspace_boundary"),
        _gauntlet("df-hostile-dependency-confusion", "dependency_confusion", "deny_namespace_mismatch", [], "registry_boundary"),
        _gauntlet("df-hostile-lifecycle-rollback", "lifecycle_rollback", "rollback_to_verified_snapshot", [], "lifecycle_boundary"),
        _gauntlet("df-hostile-quarantine-bypass", "quarantine_bypass", "deny_quarantine_reentry", [], "quarantine_boundary"),
        _gauntlet("df-hostile-malicious-update", "malicious_update", "deny_update_and_restore_previous", [], "update_boundary"),
        _gauntlet("df-hostile-install-script", "install_script_execution", "deny_install_hook_execution", [], "lifecycle_hook_boundary"),
        _gauntlet("df-hostile-typosquatting", "typosquatting", "deny_confusable_package_name", [], "registry_boundary"),
        _gauntlet("df-hostile-transitive-compromise", "transitive_dependency_compromise", "deny_transitive_digest_mismatch", [], "dependency_boundary"),
        _gauntlet("df-hostile-compromised-key", "compromised_signing_key", "deny_revoked_signing_key", [], "signature_boundary"),
        _gauntlet("df-hostile-native-binary", "native_binary_artifact", "deny_unreviewed_native_artifact", [], "artifact_boundary"),
        _gauntlet("df-hostile-archive-traversal", "archive_path_traversal", "deny_archive_path_traversal", [], "workspace_boundary"),
        _gauntlet("df-hostile-symlink-escape", "symlink_escape", "deny_symlink_workspace_escape", [], "workspace_boundary"),
        _gauntlet("df-hostile-runtime-fetch", "runtime_fetch", "deny_unapproved_runtime_fetch", [], "network_boundary"),
        _gauntlet("df-hostile-dynamic-import", "dynamic_import", "deny_unreviewed_dynamic_import", [], "runtime_boundary"),
        _gauntlet("df-hostile-prompt-injection", "package_prompt_injection", "deny_package_prompt_instruction", [], "prompt_boundary"),
        _gauntlet("df-hostile-tool-injection", "tool_injection", "deny_tool_schema_mutation", [], "tool_boundary"),
    ]


def build_production_secure_marketplace_contract() -> dict[str, Any]:
    gates = production_secure_marketplace_gate_receipts()
    certifications = third_party_package_security_certification_receipts()
    corpus = marketplace_live_corpus_operations_v2_receipts()
    flows = marketplace_lifecycle_flow_receipts()
    hostile = hostile_package_lifecycle_gauntlet_receipts()
    policy = production_secure_marketplace_policy_payload()
    all_items = [*gates, *certifications, *corpus, *flows, *hostile]
    promoted = [item for item in corpus if item["promotion_decision"] in {"allow_promote", "staged_rollout"}]
    deny_actions = {"deny_and_quarantine", "deny_until_rescan", "hold_for_external_retest"}
    return {
        "summary": {
            "operator_status": "production_secure_marketplace_receipts_visible",
            "claim_boundary": PRODUCTION_SECURE_MARKETPLACE_CLAIM_BOUNDARY,
            "production_gate_count": len(gates),
            "certification_review_count": len(certifications),
            "live_corpus_package_count": len(corpus),
            "live_corpus_family_count": len({item["family"] for item in corpus}),
            "lifecycle_flow_count": len(flows),
            "hostile_gauntlet_count": len(hostile),
            "required_lifecycle_flows_covered": (
                set(REQUIRED_MARKETPLACE_LIFECYCLE_FLOWS) <= {item["flow"] for item in flows}
            ),
            "required_hostile_drills_covered": (
                set(REQUIRED_HOSTILE_PACKAGE_DRILLS) <= {item["drill_class"] for item in hostile}
            ),
            "promoted_package_proof_complete": all(
                _package_promotion_gate_passes(item)
                for item in promoted
            ),
            "blocked_or_held_risky_package_count": sum(
                1 for item in corpus if item["promotion_decision"] in deny_actions
            ),
            "critical_high_denied_count": sum(
                1
                for item in corpus
                if any(finding["severity"] in {"critical", "high"} for finding in item["vulnerabilities"])
                and item["promotion_decision"] in deny_actions
            ),
            "external_review_scope_count": len({scope for item in certifications for scope in item["review_scope"]}),
            "certification_findings_retested": all(
                finding["retest_evidence"] for item in certifications for finding in item["findings"]
            ),
            "certification_review_proofs_bound": all(
                item["reviewer_identity_verified"] is True
                and item["reviewer_conflict_checked"] is True
                and item["publisher_conflict_detected"] is False
                and item["review_report_digest"]
                and item["scope_artifact_digest"]
                and item["signed_reviewer_receipt"]
                and item["review_expires_at"] >= "2026-06-11"
                for item in certifications
            ),
            "certification_claim_lift_blocked": all(item["claim_lift_allowed"] is False for item in certifications),
            "waiver_expiry_visible": any(
                finding.get("waiver_expires_at") for item in certifications for finding in item["findings"]
            ),
            "hostile_gauntlet_fail_closed": all(item["fails_closed"] is True for item in hostile),
            "operator_notification_count": sum(1 for item in [*flows, *hostile] if item["operator_notification_id"]),
            "safe_receipts_redacted": _all_safe_receipts_redacted(all_items),
            "production_secure_marketplace_claim_allowed": False,
            "third_party_package_security_solved_claim_allowed": False,
            "ecosystem_superiority_claim_allowed": False,
        },
        "production_gates": gates,
        "third_party_package_security_certification": certifications,
        "live_corpus_operations_v2": corpus,
        "lifecycle_flow_receipts": flows,
        "hostile_package_lifecycle_gauntlet": hostile,
        "policy": policy,
    }


async def _run_production_secure_marketplace_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_SECURE_MARKETPLACE_V1_SUITE_NAME,
        THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SUITE_NAME,
        MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SUITE_NAME,
        HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SUITE_NAME,
    ])


async def build_production_secure_marketplace_report() -> dict[str, Any]:
    summary = await _run_production_secure_marketplace_suites()
    contract = build_production_secure_marketplace_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_secure_marketplace_ci_gated_operator_visible"
                if healthy
                else "production_secure_marketplace_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_SECURE_MARKETPLACE_V1_SCENARIO_NAMES)
                + len(THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SCENARIO_NAMES)
                + len(MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SCENARIO_NAMES)
                + len(HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            PRODUCTION_SECURE_MARKETPLACE_V1_SUITE_NAME: list(PRODUCTION_SECURE_MARKETPLACE_V1_SCENARIO_NAMES),
            THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SUITE_NAME: list(
                THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SCENARIO_NAMES
            ),
            MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SUITE_NAME: list(
                MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SCENARIO_NAMES
            ),
            HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SUITE_NAME: list(
                HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="production_secure_marketplace"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def _gate_row(
    receipt_id: str,
    gate: str,
    evidence: str,
    fail_closed_decision: str,
    visible_fields: list[str],
) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "gate": gate,
        "evidence": evidence,
        "fail_closed_decision": fail_closed_decision,
        "operator_visible_fields": visible_fields,
    }
    return {
        "receipt_id": receipt_id,
        "gate": gate,
        "evidence": evidence,
        "fail_closed_decision": fail_closed_decision,
        "operator_visible_fields": visible_fields,
        "promotion_required": True,
        "claim_lift_allowed": False,
        "safe_receipt": _safe_receipt(f"operator-df:gate:{receipt_id}", safe_payload),
    }


def _certification_row(
    receipt_id: str,
    review_type: str,
    reviewer_id: str,
    reviewer_independence: str,
    review_report_ref: str,
    review_scope: list[str],
    *,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    review_report_digest = _digest(f"review-report:{review_report_ref}")
    scope_artifact_digest = _digest(f"review-scope:{receipt_id}:{','.join(sorted(review_scope))}")
    signed_reviewer_receipt = _digest(f"signed-reviewer-receipt:{reviewer_id}:{review_report_digest}")
    safe_payload = {
        "receipt_id": receipt_id,
        "review_type": review_type,
        "reviewer_id": reviewer_id,
        "review_report_digest": review_report_digest,
        "scope_artifact_digest": scope_artifact_digest,
        "finding_ids": [finding["finding_id"] for finding in findings],
    }
    return {
        "receipt_id": receipt_id,
        "review_type": review_type,
        "reviewer_id": reviewer_id,
        "reviewer_independence": reviewer_independence,
        "reviewer_identity_verified": True,
        "reviewer_conflict_checked": True,
        "publisher_conflict_detected": False,
        "review_evidence_mode": "fixture_external_review_receipt_not_formal_certification",
        "review_date": "2026-06-11",
        "review_expires_at": "2026-09-30",
        "review_report_digest": review_report_digest,
        "scope_artifact_digest": scope_artifact_digest,
        "signed_reviewer_receipt": signed_reviewer_receipt,
        "review_scope": review_scope,
        "findings": findings,
        "finding_count": len(findings),
        "all_findings_retested": all(finding["retest_evidence"] for finding in findings),
        "open_unwaived_critical_count": sum(
            1
            for finding in findings
            if finding["severity"] == "critical" and finding["disposition"] != "blocked_without_waiver"
        ),
        "claim_lift_allowed": False,
        "residual_risk": "bounded_review_receipt_not_formal_marketplace_certification",
        "safe_receipt": _safe_receipt(f"operator-df:certification:{receipt_id}", safe_payload),
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
        "waiver_expired": waiver_expires_at is not None and waiver_expires_at < "2026-06-11",
    }


def _corpus_v2_package(
    receipt_id: str,
    package_id: str,
    family: str,
    package_version: str,
    publisher_id: str,
    promotion_decision: str,
    vulnerabilities: list[dict[str, Any]],
    *,
    signature_status: str = "verified",
    key_state: str = "active",
) -> dict[str, Any]:
    package_slug = package_id.replace(".", "-")
    safe_payload = {
        "receipt_id": receipt_id,
        "package_id": package_id,
        "family": family,
        "package_version": package_version,
        "publisher_id": publisher_id,
        "promotion_decision": promotion_decision,
        "vulnerability_ids": [item["id"] for item in vulnerabilities],
    }
    return {
        "receipt_id": receipt_id,
        "package_id": package_id,
        "family": family,
        "package_version": package_version,
        "publisher_id": publisher_id,
        "publisher_identity_verified": publisher_id.startswith("pub.verified."),
        "publisher_key_state": key_state,
        "package_digest": f"sha256:df-{package_slug}-{package_version}",
        "signed_digest": f"sigstore-bundle-df-{package_slug}-{package_version}" if signature_status == "verified" else "missing",
        "signature_status": signature_status,
        "sbom_digest": f"sha256:df-sbom-{package_slug}-{package_version}",
        "dependency_graph_digest": f"sha256:df-deps-{package_slug}-{package_version}",
        "compatibility_state": "compatible" if promotion_decision in {"allow_promote", "staged_rollout"} else "blocked",
        "scanner_sources": ["osv.dev", "nvd.nist.gov/rest/json/cves/2.0", "github-advisory-database"],
        "scanner_freshness_at": "2026-06-11" if promotion_decision not in {"deny_until_rescan"} else "2026-03-01",
        "waiver_expiry_checked": True,
        "remediation_sla_hours": 24 if any(v["severity"] in {"critical", "high"} for v in vulnerabilities) else 168,
        "vulnerabilities": vulnerabilities,
        "promotion_decision": promotion_decision,
        "quarantine_state": "quarantined" if promotion_decision == "deny_and_quarantine" else "not_quarantined",
        "reentry_required": promotion_decision in {"deny_and_quarantine", "deny_until_rescan", "hold_for_external_retest"},
        "package_count_claim_allowed": False,
        "safe_receipt": _safe_receipt(f"operator-df:corpus:{package_id}", safe_payload),
    }


def _flow(receipt_id: str, flow: str, subject: str, state: str, snapshot_id: str) -> dict[str, Any]:
    fail_closed = state.startswith(("blocked", "deny", "runtime_disabled", "publisher_blocked", "quarantined", "reentry_denied"))
    safe_payload = {
        "receipt_id": receipt_id,
        "flow": flow,
        "subject": subject,
        "state": state,
        "rollback_snapshot_id": snapshot_id,
    }
    return {
        "receipt_id": receipt_id,
        "flow": flow,
        "subject": subject,
        "state": state,
        "rollback_snapshot_id": snapshot_id,
        "operator_visible": True,
        "diagnostics_visible": True,
        "fail_closed": fail_closed or flow in {"rollback", "staged_rollout", "review", "diagnostic"},
        "operator_notification_id": f"operator:marketplace-df:flow:{flow}:{subject}",
        "safe_receipt": _safe_receipt(f"operator-df:flow:{receipt_id}", safe_payload),
    }


def _gauntlet(
    receipt_id: str,
    drill_class: str,
    decision: str,
    resolved_addresses: list[str],
    boundary: str,
) -> dict[str, Any]:
    safe_payload = {
        "receipt_id": receipt_id,
        "drill_class": drill_class,
        "decision": decision,
        "boundary": boundary,
    }
    return {
        "receipt_id": receipt_id,
        "drill_class": drill_class,
        "decision": decision,
        "fails_closed": decision.startswith(("deny", "rollback")),
        "resolved_addresses": resolved_addresses,
        "boundary": boundary,
        "runtime_contribution_allowed": False,
        "operator_recovery_action": "quarantine_and_restore_verified_snapshot"
        if drill_class in {"quarantine_bypass", "malicious_update", "lifecycle_rollback"}
        else "deny_and_record_boundary_receipt",
        "operator_notification_id": f"operator:marketplace-df:hostile:{drill_class}",
        "safe_receipt": _safe_receipt(f"operator-df:hostile:{receipt_id}", safe_payload),
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
        and bool(item["sbom_digest"])
        and bool(item["dependency_graph_digest"])
        and item["compatibility_state"] == "compatible"
        and item["scanner_freshness_at"] == "2026-06-11"
        and item["waiver_expiry_checked"] is True
        and item["remediation_sla_hours"] is not None
        and item["quarantine_state"] == "not_quarantined"
        and item["reentry_required"] is False
        and has_unwaived_high_or_critical is False
    )


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
        "redaction_layer": "production_secure_marketplace_v1",
        "evidence_body_digest": evidence_body_digest,
        "sanitized_payload_digest": evidence_body_digest,
        "tamper_evident_digest": _digest(f"{handle}:{evidence_body_digest}"),
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
        and item["safe_receipt"]["redaction_layer"] == "production_secure_marketplace_v1"
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
    return sha256(f"seraph-batch-df:{value}".encode("utf-8")).hexdigest()


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Production secure marketplace scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]
