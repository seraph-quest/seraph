"""Batch CO production marketplace security and package-network receipts.

This module adds the stronger proof layer above Batch CA lifecycle receipts and
Batch CG recorded-live marketplace attestation. The receipts model hostile
package-network operations, independent package review, vulnerability-source
freshness, publisher/key trust, rollback, quarantine, and operator notification
evidence. They remain bounded proof, not production-secure marketplace,
third-party package security solved, ecosystem superiority, full parity, or
reference-system exceedance claims.
"""

from __future__ import annotations

from typing import Any


INDEPENDENT_PACKAGE_SECURITY_REVIEW_SUITE_NAME = "independent_package_security_review"
INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES = (
    "independent_package_review_scope_behavior",
    "independent_package_digest_signature_behavior",
    "independent_package_sbom_vulnerability_behavior",
    "operator_independent_package_review_surface_behavior",
)
HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SUITE_NAME = "hostile_ecosystem_package_drills"
HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES = (
    "hostile_unsigned_artifact_fail_closed_behavior",
    "hostile_digest_mismatch_fail_closed_behavior",
    "hostile_dependency_confusion_fail_closed_behavior",
    "hostile_permission_creep_quarantine_behavior",
    "hostile_compromised_key_rotation_behavior",
    "hostile_unsafe_lifecycle_hook_behavior",
    "hostile_suspicious_transitive_dependency_behavior",
    "hostile_compatibility_migration_failure_behavior",
)
PACKAGE_NETWORK_INCIDENT_OPERATIONS_SUITE_NAME = "package_network_incident_operations"
PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES = (
    "package_network_private_ssrf_denial_behavior",
    "package_network_redirect_to_private_denial_behavior",
    "package_network_dns_private_resolution_behavior",
    "package_network_secret_ref_injection_denial_behavior",
    "package_network_workspace_escape_denial_behavior",
    "package_network_url_drift_after_review_denial_behavior",
    "package_network_rollback_after_egress_attempt_behavior",
)
PUBLISHER_TRUST_VULNERABILITY_HANDLING_SUITE_NAME = "publisher_trust_vulnerability_handling"
PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES = (
    "publisher_identity_key_freshness_behavior",
    "publisher_revocation_and_stale_review_behavior",
    "vulnerability_database_freshness_behavior",
    "vulnerability_severity_remediation_waiver_behavior",
    "operator_publisher_vulnerability_surface_behavior",
)
MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SUITE_NAME = "marketplace_rollback_quarantine_diagnostics"
MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES = (
    "marketplace_install_snapshot_behavior",
    "marketplace_update_failed_rollback_behavior",
    "marketplace_downgrade_review_hold_behavior",
    "marketplace_quarantine_runtime_cutoff_behavior",
    "marketplace_reentry_denied_behavior",
    "marketplace_incident_notification_behavior",
    "marketplace_durable_restore_point_behavior",
)

PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY = (
    "bounded_production_marketplace_security_receipts_not_production_secure_marketplace_or_ecosystem_superiority"
)
PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS = (
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


def production_marketplace_security_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            INDEPENDENT_PACKAGE_SECURITY_REVIEW_SUITE_NAME,
            HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SUITE_NAME,
            PACKAGE_NETWORK_INCIDENT_OPERATIONS_SUITE_NAME,
            PUBLISHER_TRUST_VULNERABILITY_HANDLING_SUITE_NAME,
            MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SUITE_NAME,
        ],
        "foundation_suites": [
            "marketplace_grade_capability_lifecycle",
            "third_party_marketplace_attestation",
            "publisher_review_and_package_trust",
            "independent_secure_host_review",
        ],
        "claim_boundary": PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
        "blocked_claims": list(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/production-marketplace-security",
            "/api/operator/live-marketplace-attestation-proof",
            "/api/operator/marketplace-lifecycle-maturity",
            "/api/operator/benchmark-proof",
            "/api/operator/final-parity-readiness-report",
        ],
        "review_policy": (
            "package promotion requires independent reviewer metadata, digest and signature checks, "
            "SBOM/dependency graph evidence, vulnerability-source freshness, publisher/key freshness, "
            "raw report locations, and residual exposure statements"
        ),
        "hostile_package_policy": (
            "unsigned, tampered, permission-expanding, dependency-confused, compromised-key, unsafe-hook, "
            "or suspicious-transitive packages fail closed or quarantine before runtime contribution"
        ),
        "package_network_policy": (
            "package-controlled URLs, redirects, DNS results, secret refs, and workspace access are treated as "
            "hostile until endpoint allowlists, private-network denial, credential-destination checks, and "
            "rollback/quarantine receipts prove bounded handling"
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


def independent_package_security_review_receipts() -> list[dict[str, Any]]:
    return [
        _package_review(
            "co-review-github-reviewer-2026-06",
            "marketplace.github-reviewer",
            "2.1.0",
            "sha256:31a0f4a6d2b9c7e4reviewedgithub",
            "sigstore-bundle-2026-06-github-reviewer",
            "fulcio-key-neurion-2026q2",
            "active",
            "pub.verified.neurion",
            "verified",
            "reviewer.marketplace.security.independent.01",
            "independent_external_fixture",
            "osv.dev / osv-scanner 1.9.1",
            "2026-06-10",
            "allow_reviewed_install",
            [],
            residual_exposure="medium_dependency_warning_requires_canary",
        ),
        _package_review(
            "co-review-browser-runner-2026-06",
            "marketplace.browser-runner",
            "3.4.2",
            "sha256:7a91b83d0aa7c1browserrunner",
            "sigstore-bundle-2026-06-browser-runner",
            "fulcio-key-seraphlabs-2026q2",
            "rotated_with_transparency_receipt",
            "pub.verified.seraph-labs",
            "verified",
            "reviewer.marketplace.security.independent.02",
            "independent_external_fixture",
            "nvd.nist.gov cves/2.0 / osv.dev",
            "2026-06-10",
            "hold_for_canary",
            [
                {
                    "id": "GHSA-fixture-medium-browser-sandbox",
                    "severity": "medium",
                    "state": "mitigated_with_profile_partitioning",
                },
            ],
            residual_exposure="browser_profile_and_network_surface_requires_partitioned_canary",
        ),
        _package_review(
            "co-review-suspicious-exporter-2026-06",
            "marketplace.suspicious-exporter",
            "0.9.5",
            "sha256:badc0ffeeunsignedexporter",
            "missing",
            "publisher-key-missing",
            "missing",
            "pub.unverified.unknown",
            "unverified",
            "reviewer.marketplace.security.independent.01",
            "independent_external_fixture",
            "osv.dev / osv-scanner 1.9.1",
            "2026-06-10",
            "deny_and_quarantine",
            [
                {"id": "CVE-fixture-critical-exporter", "severity": "critical", "state": "unwaived"},
            ],
            signature_status="missing",
            residual_exposure="critical_unwaived_vulnerability_and_unsigned_archive",
        ),
        _package_review(
            "co-review-voice-summary-2026-06",
            "marketplace.voice-summary",
            "1.8.1",
            "sha256:54c0f126voicesummarysafe",
            "sigstore-bundle-2026-06-voice-summary",
            "fulcio-key-media-2026q2",
            "active",
            "pub.verified.media",
            "verified",
            "reviewer.marketplace.security.independent.03",
            "separate_reviewer_from_publisher",
            "osv.dev / nvd.nist.gov cves/2.0",
            "2026-06-10",
            "allow_reviewed_install",
            [],
            residual_exposure="voice_media_redaction_and_transcript_scope_monitoring_required",
        ),
    ]


def hostile_ecosystem_package_drill_receipts() -> list[dict[str, Any]]:
    return [
        _hostile_drill("co-hostile-unsigned-artifact", "unsigned_artifact", "blocked", ["signature_missing"]),
        _hostile_drill(
            "co-hostile-digest-mismatch",
            "digest_mismatch",
            "blocked",
            ["package_digest_mismatch", "signed_digest_mismatch"],
        ),
        _hostile_drill(
            "co-hostile-dependency-confusion",
            "dependency_confusion",
            "blocked",
            ["registry_namespace_mismatch", "typosquat_namespace"],
        ),
        _hostile_drill(
            "co-hostile-permission-creep",
            "permission_creep",
            "quarantined",
            ["new_network_permission", "reviewed_permission_envelope_exceeded"],
        ),
        _hostile_drill(
            "co-hostile-compromised-key",
            "compromised_key",
            "blocked",
            ["key_revoked", "key_rotation_stale", "transparency_log_mismatch"],
        ),
        _hostile_drill(
            "co-hostile-unsafe-lifecycle-hook",
            "unsafe_lifecycle_hook",
            "blocked",
            ["postinstall_network_call", "workspace_secret_scan_attempt"],
        ),
        _hostile_drill(
            "co-hostile-suspicious-transitive",
            "suspicious_transitive_dependency",
            "blocked",
            ["unexpected_native_binary", "unknown_maintainer_namespace"],
        ),
        _hostile_drill(
            "co-hostile-compatibility-migration",
            "compatibility_migration_failure",
            "rolled_back",
            ["runtime_abi_mismatch", "migration_validation_failed"],
        ),
    ]


def package_network_incident_receipts() -> list[dict[str, Any]]:
    return [
        _network_incident(
            "co-network-loopback-ssrf",
            "private_network_ssrf",
            "https://127.0.0.1:3000/admin",
            ["127.0.0.1"],
            "deny_private_network",
            ["quarantine_package", "notify_operator"],
        ),
        _network_incident(
            "co-network-rfc1918-redirect",
            "redirect_to_private_network",
            "https://updates.example.invalid/package",
            ["203.0.113.10", "10.0.0.8"],
            "deny_redirect_chain",
            ["rollback_candidate", "preserve_redirect_receipt"],
        ),
        _network_incident(
            "co-network-dns-rebind",
            "dns_private_resolution",
            "https://cdn.marketplace.example.invalid",
            ["198.51.100.17", "169.254.169.254"],
            "deny_private_resolution",
            ["block_resolution", "open_incident"],
        ),
        _network_incident(
            "co-network-secret-ref-injection",
            "secret_ref_injection",
            "https://collector.example.invalid/upload",
            ["198.51.100.24"],
            "deny_non_allowlisted_secret_ref",
            ["redact_secret_ref", "rotate_secret_ref"],
            secret_ref_policy="destination_host_mismatch_denied",
        ),
        _network_incident(
            "co-network-workspace-escape",
            "workspace_escape_attempt",
            "file:///workspace/../.env",
            [],
            "deny_workspace_escape",
            ["block_file_read", "quarantine_runtime"],
            workspace_egress_decision="path_traversal_denied",
        ),
        _network_incident(
            "co-network-rollback-after-egress",
            "egress_attempt_after_review",
            "https://new-endpoint.example.invalid/telemetry",
            ["203.0.113.44"],
            "deny_url_drift_after_review",
            ["restore_previous_digest", "disable_runtime_contribution"],
        ),
    ]


def publisher_trust_vulnerability_receipts() -> list[dict[str, Any]]:
    return [
        _publisher_vulnerability(
            "co-publisher-neurion-current",
            "pub.verified.neurion",
            "verified",
            "fulcio-key-neurion-2026q2",
            "active",
            "2026-06-10",
            "current",
            "osv.dev",
            "2026-06-10",
            "allow_reviewed_install",
            [],
        ),
        _publisher_vulnerability(
            "co-publisher-seraphlabs-rotated",
            "pub.verified.seraph-labs",
            "verified",
            "fulcio-key-seraphlabs-2026q2",
            "rotated_with_transparency_receipt",
            "2026-06-10",
            "current_with_warning",
            "nvd.nist.gov/rest/json/cves/2.0",
            "2026-06-10",
            "hold_for_canary",
            [{"id": "GHSA-fixture-medium-browser-sandbox", "severity": "medium", "waiver": "expires_2026-06-30"}],
        ),
        _publisher_vulnerability(
            "co-publisher-unknown-revoked",
            "pub.unverified.unknown",
            "unverified",
            "publisher-key-missing",
            "revoked_or_missing",
            "2026-04-01",
            "stale_or_missing",
            "osv.dev",
            "2026-06-10",
            "deny_and_quarantine",
            [{"id": "CVE-fixture-critical-exporter", "severity": "critical", "waiver": "missing"}],
        ),
        _publisher_vulnerability(
            "co-publisher-media-current",
            "pub.verified.media",
            "verified",
            "fulcio-key-media-2026q2",
            "active",
            "2026-06-10",
            "current",
            "osv.dev+nvd.nist.gov/rest/json/cves/2.0",
            "2026-06-10",
            "allow_reviewed_install",
            [],
        ),
        _publisher_vulnerability(
            "co-vulnerability-stale-db-negative",
            "pub.verified.legacy",
            "verified",
            "fulcio-key-legacy-2025q4",
            "stale_rotation",
            "2025-12-15",
            "stale_review",
            "scanner_fixture_stale_db",
            "2026-03-01",
            "deny_until_rescan",
            [{"id": "CVE-fixture-high-stale-db", "severity": "high", "waiver": "expired"}],
        ),
    ]


def rollback_quarantine_diagnostic_receipts() -> list[dict[str, Any]]:
    return [
        _lifecycle_diagnostic("co-lifecycle-install-snapshot", "install", "installed_after_review", "snapshot-install-001"),
        _lifecycle_diagnostic("co-lifecycle-update-rollback", "update", "rolled_back", "snapshot-browser-341"),
        _lifecycle_diagnostic("co-lifecycle-downgrade-hold", "downgrade", "blocked_until_review", "snapshot-voice-181"),
        _lifecycle_diagnostic("co-lifecycle-quarantine-cutoff", "quarantine", "runtime_cut_off", "snapshot-exporter-095"),
        _lifecycle_diagnostic("co-lifecycle-reentry-denied", "reentry_review", "reentry_denied", "snapshot-exporter-095"),
        _lifecycle_diagnostic("co-lifecycle-incident-notification", "incident_notification", "operator_notified", "snapshot-incident-024"),
        _lifecycle_diagnostic("co-lifecycle-durable-restore", "rollback", "durable_restore_point_verified", "snapshot-browser-340"),
    ]


def production_marketplace_security_receipt_matrix() -> list[dict[str, Any]]:
    matrix: list[dict[str, Any]] = []
    for receipt in independent_package_security_review_receipts():
        matrix.append(_matrix_row(receipt, incident_class="independent_review", lifecycle_action="review"))
    for receipt in hostile_ecosystem_package_drill_receipts():
        matrix.append(_matrix_row(receipt, incident_class=receipt["drill_class"], lifecycle_action="hostile_drill"))
    for receipt in package_network_incident_receipts():
        matrix.append(_matrix_row(receipt, incident_class=receipt["package_network_incident_class"], lifecycle_action="network_incident"))
    for receipt in publisher_trust_vulnerability_receipts():
        matrix.append(_matrix_row(receipt, incident_class="publisher_vulnerability", lifecycle_action="trust_review"))
    for receipt in rollback_quarantine_diagnostic_receipts():
        matrix.append(_matrix_row(receipt, incident_class="rollback_quarantine", lifecycle_action=receipt["lifecycle_action"]))
    return matrix


def build_production_marketplace_security_contract() -> dict[str, Any]:
    reviews = independent_package_security_review_receipts()
    drills = hostile_ecosystem_package_drill_receipts()
    incidents = package_network_incident_receipts()
    publisher_vulnerability = publisher_trust_vulnerability_receipts()
    diagnostics = rollback_quarantine_diagnostic_receipts()
    matrix = production_marketplace_security_receipt_matrix()
    policy = production_marketplace_security_policy_payload()
    deny_or_quarantine_actions = {"blocked", "quarantined", "rolled_back", "deny_and_quarantine", "deny_until_rescan"}
    return {
        "summary": {
            "operator_status": "production_marketplace_security_receipts_visible",
            "independent_package_review_count": len(reviews),
            "hostile_drill_count": len(drills),
            "package_network_incident_count": len(incidents),
            "publisher_vulnerability_review_count": len(publisher_vulnerability),
            "rollback_quarantine_diagnostic_count": len(diagnostics),
            "receipt_matrix_count": len(matrix),
            "independent_reviewer_count": len({item["reviewer_id"] for item in reviews}),
            "sbom_dependency_digest_count": sum(
                1 for item in reviews if item.get("sbom_digest") and item.get("dependency_graph_digest")
            ),
            "scanner_source_count": len({item["vulnerability_source"] for item in publisher_vulnerability}),
            "vulnerability_database_freshness_visible": all(
                item.get("database_freshness_at") for item in publisher_vulnerability
            ),
            "private_network_ssrf_denied_count": sum(
                1
                for item in incidents
                if "private" in item["private_network_decision"]
                or item["package_network_incident_class"] == "redirect_to_private_network"
            ),
            "secret_ref_denied_count": sum(
                1 for item in incidents if "secret_ref" in item["package_network_incident_class"]
            ),
            "workspace_escape_denied_count": sum(
                1 for item in incidents if item.get("workspace_egress_decision") == "path_traversal_denied"
            ),
            "fail_closed_hostile_count": sum(1 for item in drills if item["decision"] in deny_or_quarantine_actions),
            "critical_unwaived_vulnerability_blocked_count": sum(
                1
                for item in publisher_vulnerability
                for finding in item["findings"]
                if finding.get("severity") in {"high", "critical"}
                and finding.get("waiver") in {"missing", "expired"}
                and item["operator_action"] in {"deny_and_quarantine", "deny_until_rescan"}
            ),
            "rollback_snapshot_count": sum(1 for item in diagnostics if item.get("rollback_snapshot_id")),
            "quarantine_reentry_count": sum(
                1 for item in diagnostics if item["quarantine_state"] in {"quarantined", "reentry_denied"}
            ),
            "operator_notification_count": sum(
                1 for item in matrix if str(item.get("operator_notification", "")).startswith("operator:")
            ),
            "production_secure_marketplace_claim_allowed": False,
            "claim_boundary": PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
        },
        "independent_package_reviews": reviews,
        "hostile_drills": drills,
        "package_network_incidents": incidents,
        "publisher_vulnerability_reviews": publisher_vulnerability,
        "rollback_quarantine_diagnostics": diagnostics,
        "receipt_matrix": matrix,
        "policy": policy,
    }


def _package_review(
    receipt_id: str,
    package_id: str,
    package_version: str,
    package_digest: str,
    signed_digest: str,
    key_id: str,
    key_state: str,
    publisher_id: str,
    publisher_identity_status: str,
    reviewer_id: str,
    reviewer_independence: str,
    vulnerability_source: str,
    database_freshness_at: str,
    operator_action: str,
    findings: list[dict[str, Any]],
    *,
    signature_status: str = "verified",
    residual_exposure: str,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "suite": INDEPENDENT_PACKAGE_SECURITY_REVIEW_SUITE_NAME,
        "evidence_mode": "independent_package_security_review_fixture",
        "package_id": package_id,
        "package_version": package_version,
        "package_digest": package_digest,
        "signed_digest": signed_digest,
        "signature_status": signature_status,
        "key_id": key_id,
        "key_state": key_state,
        "publisher_id": publisher_id,
        "publisher_identity_status": publisher_identity_status,
        "publisher_review_date": "2026-06-10" if publisher_identity_status == "verified" else "2026-04-01",
        "reviewer_id": reviewer_id,
        "reviewer_independence": reviewer_independence,
        "review_scope": [
            "provenance",
            "signature",
            "publisher_identity",
            "sbom",
            "dependency_graph",
            "vulnerability_policy",
            "package_network_boundary",
            "rollback_quarantine",
        ],
        "sbom_digest": f"sha256:sbom-{package_id.replace('.', '-')}-{package_version}",
        "dependency_graph_digest": f"sha256:deps-{package_id.replace('.', '-')}-{package_version}",
        "registry_namespace": package_id.split(".")[0],
        "source_url": f"https://marketplace.example.invalid/{package_id}/{package_version}",
        "allowed_endpoints": ["https://api.github.com", "https://osv.dev", "https://services.nvd.nist.gov"],
        "observed_endpoints": [],
        "redirect_chain": [],
        "resolved_addresses": [],
        "private_network_decision": "not_applicable_review_receipt",
        "secret_ref_policy": "not_applicable_review_receipt",
        "workspace_egress_decision": "not_applicable_review_receipt",
        "vulnerability_source": vulnerability_source,
        "scanner_version": "osv-scanner 1.9.1 / nvd-cve-api-2.0",
        "database_freshness_at": database_freshness_at,
        "query_time": "2026-06-10T09:00:00Z",
        "severity_policy": "critical_or_high_requires_remediation_or_current_exception",
        "findings": findings,
        "remediation_path": "upgrade_patch_or_quarantine_before_runtime_contribution",
        "waiver_path": "time_limited_security_exception_with_operator_approval",
        "residual_exposure": residual_exposure,
        "lifecycle_action": "review",
        "rollback_snapshot_id": f"snapshot-{package_id.replace('.', '-')}-{package_version}",
        "quarantine_state": "quarantined" if operator_action == "deny_and_quarantine" else "not_quarantined",
        "reentry_decision": "fresh_review_required",
        "operator_notification_id": f"operator:marketplace-co:review:{package_id}",
        "raw_receipt_location": f"artifacts/operator-co/package-reviews/{receipt_id}.json",
        "failure_budget": "zero_critical_unwaived_zero_unsigned_promotions",
        "blocked_claims": list(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS),
        "claim_boundary": PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
        "operator_action": operator_action,
    }


def _hostile_drill(receipt_id: str, drill_class: str, decision: str, triggers: list[str]) -> dict[str, Any]:
    package_id = f"marketplace.hostile.{drill_class.replace('_', '-')}"
    return {
        "receipt_id": receipt_id,
        "suite": HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SUITE_NAME,
        "evidence_mode": "hostile_package_drill_fixture",
        "package_id": package_id,
        "package_version": "0.0.1",
        "package_digest": f"sha256:hostile-{drill_class}",
        "signed_digest": "missing" if drill_class == "unsigned_artifact" else f"sha256:signed-hostile-{drill_class}",
        "signature_status": "missing" if drill_class == "unsigned_artifact" else "suspicious",
        "key_id": "publisher-key-hostile",
        "key_state": "revoked" if drill_class == "compromised_key" else "untrusted",
        "publisher_id": "pub.unverified.hostile",
        "publisher_identity_status": "unverified",
        "publisher_review_date": "2026-04-01",
        "reviewer_id": "reviewer.marketplace.security.independent.chaos",
        "reviewer_independence": "independent_external_fixture",
        "review_scope": ["hostile_package_behavior", "fail_closed_decision", "operator_notification"],
        "sbom_digest": f"sha256:sbom-hostile-{drill_class}",
        "dependency_graph_digest": f"sha256:deps-hostile-{drill_class}",
        "registry_namespace": "hostile-fixture",
        "source_url": "https://registry.evil.example.invalid/package.tgz",
        "allowed_endpoints": [],
        "observed_endpoints": ["https://collector.evil.example.invalid"],
        "redirect_chain": [],
        "resolved_addresses": ["203.0.113.200"],
        "private_network_decision": "no_private_network_access_attempted",
        "secret_ref_policy": "secret_ref_not_exposed",
        "workspace_egress_decision": "runtime_contribution_blocked",
        "vulnerability_source": "hostile_fixture_catalog",
        "scanner_version": "hostile-package-drill-1.0",
        "database_freshness_at": "2026-06-10",
        "query_time": "2026-06-10T09:05:00Z",
        "severity_policy": "hostile_or_unknown_fails_closed",
        "findings": [{"id": trigger, "severity": "critical" if "critical" in trigger else "high"} for trigger in triggers],
        "remediation_path": "block_quarantine_or_restore_previous_digest",
        "waiver_path": "no_waiver_for_hostile_fixture",
        "residual_exposure": "package_never_promoted_to_runtime_contribution",
        "lifecycle_action": "hostile_drill",
        "rollback_snapshot_id": f"snapshot-hostile-{drill_class}",
        "quarantine_state": "quarantined" if decision in {"quarantined", "rolled_back"} else "blocked_before_install",
        "reentry_decision": "denied_until_independent_review",
        "operator_notification_id": f"operator:marketplace-co:hostile:{drill_class}",
        "raw_receipt_location": f"artifacts/operator-co/hostile-drills/{receipt_id}.json",
        "failure_budget": "zero_hostile_promotions",
        "blocked_claims": list(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS),
        "claim_boundary": PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
        "drill_class": drill_class,
        "decision": decision,
        "triggers": triggers,
        "fails_closed": True,
    }


def _network_incident(
    receipt_id: str,
    incident_class: str,
    endpoint: str,
    resolved_addresses: list[str],
    decision: str,
    recovery_actions: list[str],
    *,
    secret_ref_policy: str = "secret_ref_not_present",
    workspace_egress_decision: str = "workspace_access_not_requested",
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "suite": PACKAGE_NETWORK_INCIDENT_OPERATIONS_SUITE_NAME,
        "evidence_mode": "package_network_incident_fixture",
        "package_id": "marketplace.browser-runner" if "workspace" not in incident_class else "marketplace.suspicious-exporter",
        "package_version": "3.4.2",
        "package_digest": "sha256:7a91b83d0aa7c1browserrunner",
        "signed_digest": "sigstore-bundle-2026-06-browser-runner",
        "signature_status": "verified_before_incident",
        "key_id": "fulcio-key-seraphlabs-2026q2",
        "key_state": "rotated_with_transparency_receipt",
        "publisher_id": "pub.verified.seraph-labs",
        "publisher_identity_status": "verified",
        "publisher_review_date": "2026-06-10",
        "reviewer_id": "reviewer.marketplace.security.independent.02",
        "reviewer_independence": "independent_external_fixture",
        "review_scope": ["network_boundary", "secret_ref_policy", "workspace_boundary", "rollback"],
        "sbom_digest": "sha256:sbom-marketplace-browser-runner-3.4.2",
        "dependency_graph_digest": "sha256:deps-marketplace-browser-runner-3.4.2",
        "registry_namespace": "marketplace",
        "source_url": "https://marketplace.example.invalid/marketplace.browser-runner/3.4.2",
        "allowed_endpoints": ["https://api.github.com", "https://osv.dev", "https://services.nvd.nist.gov"],
        "observed_endpoints": [endpoint],
        "redirect_chain": [endpoint] if "redirect" in incident_class else [],
        "resolved_addresses": resolved_addresses,
        "private_network_decision": decision,
        "secret_ref_policy": secret_ref_policy,
        "workspace_egress_decision": workspace_egress_decision,
        "vulnerability_source": "package_network_incident_fixture",
        "scanner_version": "site-policy-ssrf-drill-1.0",
        "database_freshness_at": "2026-06-10",
        "query_time": "2026-06-10T09:10:00Z",
        "severity_policy": "private_network_secret_or_workspace_boundary_violation_fails_closed",
        "findings": [{"id": incident_class, "severity": "critical", "decision": decision}],
        "remediation_path": "deny_request_quarantine_or_restore_previous_digest",
        "waiver_path": "no_waiver_for_private_network_secret_or_workspace_boundary_violation",
        "residual_exposure": "incident_blocked_before_payload_or_secret_release",
        "package_network_incident_class": incident_class,
        "affected_endpoint_host": endpoint,
        "lifecycle_action": "network_incident",
        "rollback_snapshot_id": f"snapshot-network-{incident_class}",
        "quarantine_state": "quarantined",
        "reentry_decision": "fresh_review_required_after_incident",
        "operator_notification_id": f"operator:marketplace-co:network:{incident_class}",
        "raw_receipt_location": f"artifacts/operator-co/package-network/{receipt_id}.json",
        "failure_budget": "zero_private_network_secret_workspace_egress",
        "recovery_actions": recovery_actions,
        "blocked_claims": list(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS),
        "claim_boundary": PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
    }


def _publisher_vulnerability(
    receipt_id: str,
    publisher_id: str,
    identity_status: str,
    key_id: str,
    key_state: str,
    publisher_review_date: str,
    review_state: str,
    vulnerability_source: str,
    database_freshness_at: str,
    operator_action: str,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "suite": PUBLISHER_TRUST_VULNERABILITY_HANDLING_SUITE_NAME,
        "evidence_mode": "publisher_vulnerability_fixture",
        "publisher_id": publisher_id,
        "publisher_identity_status": identity_status,
        "key_id": key_id,
        "key_state": key_state,
        "publisher_review_date": publisher_review_date,
        "review_state": review_state,
        "reviewer_id": "reviewer.marketplace.security.independent.publisher",
        "reviewer_independence": "independent_external_fixture",
        "review_scope": ["publisher_identity", "key_rotation", "review_freshness", "vulnerability_policy"],
        "package_id": "publisher_scope",
        "package_version": "multiple",
        "package_digest": "publisher_scope",
        "signed_digest": "publisher_scope",
        "signature_status": "publisher_scope",
        "sbom_digest": "publisher_scope",
        "dependency_graph_digest": "publisher_scope",
        "registry_namespace": "marketplace",
        "source_url": f"https://marketplace.example.invalid/publishers/{publisher_id}",
        "allowed_endpoints": ["https://osv.dev", "https://services.nvd.nist.gov"],
        "observed_endpoints": [],
        "redirect_chain": [],
        "resolved_addresses": [],
        "private_network_decision": "not_applicable_publisher_review",
        "secret_ref_policy": "not_applicable_publisher_review",
        "workspace_egress_decision": "not_applicable_publisher_review",
        "vulnerability_source": vulnerability_source,
        "scanner_version": "osv-scanner 1.9.1 / nvd-cve-api-2.0",
        "database_freshness_at": database_freshness_at,
        "query_time": "2026-06-10T09:15:00Z",
        "severity_policy": "critical_or_high_requires_remediation_or_current_exception",
        "findings": findings,
        "remediation_path": "patch_upgrade_quarantine_or_disable_runtime_contribution",
        "waiver_path": "time_limited_exception_with_expiry_and_operator_notification",
        "residual_exposure": "bounded_by_review_freshness_key_state_and_vulnerability_policy",
        "lifecycle_action": "publisher_vulnerability_review",
        "rollback_snapshot_id": f"snapshot-publisher-{publisher_id}",
        "quarantine_state": "quarantined" if operator_action in {"deny_and_quarantine", "deny_until_rescan"} else "not_quarantined",
        "reentry_decision": "fresh_review_required" if "deny" in operator_action else "allowed_with_policy",
        "operator_notification_id": f"operator:marketplace-co:publisher:{publisher_id}",
        "raw_receipt_location": f"artifacts/operator-co/publisher-vulnerability/{receipt_id}.json",
        "failure_budget": "zero_stale_db_high_or_critical_promotions",
        "blocked_claims": list(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS),
        "claim_boundary": PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
        "operator_action": operator_action,
    }


def _lifecycle_diagnostic(receipt_id: str, action: str, state: str, snapshot_id: str) -> dict[str, Any]:
    package_id = "marketplace.browser-runner" if action in {"install", "update", "rollback"} else "marketplace.suspicious-exporter"
    return {
        "receipt_id": receipt_id,
        "suite": MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SUITE_NAME,
        "evidence_mode": "rollback_quarantine_diagnostic_fixture",
        "package_id": package_id,
        "package_version": "3.4.2" if package_id == "marketplace.browser-runner" else "0.9.5",
        "package_digest": "sha256:7a91b83d0aa7c1browserrunner" if package_id == "marketplace.browser-runner" else "sha256:badc0ffeeunsignedexporter",
        "signed_digest": "sigstore-bundle-2026-06-browser-runner" if package_id == "marketplace.browser-runner" else "missing",
        "signature_status": "verified" if package_id == "marketplace.browser-runner" else "missing",
        "key_id": "fulcio-key-seraphlabs-2026q2" if package_id == "marketplace.browser-runner" else "publisher-key-missing",
        "key_state": "rotated_with_transparency_receipt" if package_id == "marketplace.browser-runner" else "missing",
        "publisher_id": "pub.verified.seraph-labs" if package_id == "marketplace.browser-runner" else "pub.unverified.unknown",
        "publisher_identity_status": "verified" if package_id == "marketplace.browser-runner" else "unverified",
        "publisher_review_date": "2026-06-10",
        "reviewer_id": "reviewer.marketplace.security.independent.diagnostics",
        "reviewer_independence": "independent_external_fixture",
        "review_scope": ["lifecycle_action", "rollback_snapshot", "quarantine_reentry", "operator_notification"],
        "sbom_digest": f"sha256:sbom-{package_id.replace('.', '-')}",
        "dependency_graph_digest": f"sha256:deps-{package_id.replace('.', '-')}",
        "registry_namespace": "marketplace",
        "source_url": f"https://marketplace.example.invalid/{package_id}",
        "allowed_endpoints": ["https://api.github.com", "https://osv.dev"],
        "observed_endpoints": [],
        "redirect_chain": [],
        "resolved_addresses": [],
        "private_network_decision": "not_applicable_lifecycle_diagnostic",
        "secret_ref_policy": "secret_refs_redacted_from_diagnostics",
        "workspace_egress_decision": "workspace_paths_redacted_from_diagnostics",
        "vulnerability_source": "osv.dev+nvd.nist.gov/rest/json/cves/2.0",
        "scanner_version": "osv-scanner 1.9.1 / nvd-cve-api-2.0",
        "database_freshness_at": "2026-06-10",
        "query_time": "2026-06-10T09:20:00Z",
        "severity_policy": "diagnostics_preserve_vulnerability_and_waiver_state",
        "findings": [],
        "remediation_path": "restore_snapshot_quarantine_or_retry_after_fresh_review",
        "waiver_path": "operator_visible_exception_required",
        "residual_exposure": "diagnostic_receipt_only_not_production_secure_marketplace",
        "lifecycle_action": action,
        "rollback_snapshot_id": snapshot_id,
        "quarantine_state": "quarantined" if state in {"runtime_cut_off", "reentry_denied"} else "not_quarantined",
        "reentry_decision": "denied" if state == "reentry_denied" else "not_requested",
        "operator_notification_id": f"operator:marketplace-co:lifecycle:{action}",
        "raw_receipt_location": f"artifacts/operator-co/rollback-quarantine/{receipt_id}.json",
        "failure_budget": "zero_failed_update_without_restore_point",
        "blocked_claims": list(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS),
        "claim_boundary": PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
        "state": state,
    }


def _matrix_row(receipt: dict[str, Any], *, incident_class: str, lifecycle_action: str) -> dict[str, Any]:
    return {
        "receipt_id": receipt["receipt_id"],
        "suite": receipt["suite"],
        "package_id": receipt.get("package_id"),
        "package_version": receipt.get("package_version"),
        "package_digest": receipt.get("package_digest"),
        "signed_digest": receipt.get("signed_digest"),
        "key_id": receipt.get("key_id"),
        "publisher_id": receipt.get("publisher_id"),
        "publisher_review_date": receipt.get("publisher_review_date"),
        "reviewer_independence": receipt.get("reviewer_independence"),
        "sbom_digest": receipt.get("sbom_digest"),
        "dependency_graph_digest": receipt.get("dependency_graph_digest"),
        "vulnerability_scanner_source": receipt.get("vulnerability_source"),
        "vulnerability_database_freshness_date": receipt.get("database_freshness_at"),
        "severity_policy": receipt.get("severity_policy"),
        "remediation_path": receipt.get("remediation_path"),
        "exception_waiver_path": receipt.get("waiver_path"),
        "package_network_incident_class": incident_class,
        "affected_endpoint_host": receipt.get("affected_endpoint_host") or receipt.get("source_url"),
        "private_network_ssrf_decision": receipt.get("private_network_decision"),
        "lifecycle_action": lifecycle_action,
        "rollback_snapshot_id": receipt.get("rollback_snapshot_id"),
        "quarantine_reentry_state": receipt.get("quarantine_state"),
        "operator_notification": receipt.get("operator_notification_id"),
        "raw_receipt_location": receipt.get("raw_receipt_location"),
        "failure_budget": receipt.get("failure_budget"),
        "residual_exposure": receipt.get("residual_exposure"),
        "blocked_claims": receipt.get("blocked_claims"),
        "claim_boundary": receipt.get("claim_boundary"),
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Production marketplace security scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_production_marketplace_security_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        INDEPENDENT_PACKAGE_SECURITY_REVIEW_SUITE_NAME,
        HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SUITE_NAME,
        PACKAGE_NETWORK_INCIDENT_OPERATIONS_SUITE_NAME,
        PUBLISHER_TRUST_VULNERABILITY_HANDLING_SUITE_NAME,
        MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SUITE_NAME,
    ])


async def build_production_marketplace_security_report() -> dict[str, Any]:
    summary = await _run_production_marketplace_security_suites()
    contract = build_production_marketplace_security_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_marketplace_security_ci_gated_operator_visible"
                if healthy
                else "production_marketplace_security_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES)
                + len(HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES)
                + len(PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES)
                + len(PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES)
                + len(MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            INDEPENDENT_PACKAGE_SECURITY_REVIEW_SUITE_NAME: list(
                INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES
            ),
            HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SUITE_NAME: list(
                HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES
            ),
            PACKAGE_NETWORK_INCIDENT_OPERATIONS_SUITE_NAME: list(
                PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES
            ),
            PUBLISHER_TRUST_VULNERABILITY_HANDLING_SUITE_NAME: list(
                PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES
            ),
            MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SUITE_NAME: list(
                MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="production_marketplace_security"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
