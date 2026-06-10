"""Batch CX marketplace registry corpus and continuous security receipts.

This module extends Batch CO's bounded marketplace-security receipts into a
larger registry/corpus operations layer with continuous scanner monitoring,
publisher trust operations, lifecycle diagnostics, and package-network denial
receipts. It remains bounded evidence, not production-secure marketplace,
solved third-party package security, ecosystem superiority, full marketplace
parity, production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

from typing import Any


MARKETPLACE_SECURITY_CORPUS_SUITE_NAME = "marketplace_security_corpus_v1"
MARKETPLACE_SECURITY_CORPUS_SCENARIO_NAMES = (
    "marketplace_corpus_inventory_behavior",
    "marketplace_corpus_provenance_signature_sbom_behavior",
    "marketplace_corpus_review_state_behavior",
    "marketplace_corpus_operation_diagnostics_behavior",
    "marketplace_corpus_receipt_safety_behavior",
)
CONTINUOUS_VULNERABILITY_MONITORING_SUITE_NAME = "continuous_vulnerability_monitoring"
CONTINUOUS_VULNERABILITY_MONITORING_SCENARIO_NAMES = (
    "continuous_vulnerability_source_freshness_behavior",
    "continuous_scanner_integration_behavior",
    "continuous_waiver_expiration_behavior",
    "continuous_remediation_sla_behavior",
    "continuous_critical_block_behavior",
)
PUBLISHER_TRUST_OPERATIONS_SUITE_NAME = "publisher_trust_operations"
PUBLISHER_TRUST_OPERATIONS_SCENARIO_NAMES = (
    "publisher_ops_identity_key_rotation_behavior",
    "publisher_ops_review_freshness_behavior",
    "publisher_ops_quarantine_reentry_behavior",
    "publisher_ops_network_secret_workspace_denial_behavior",
    "publisher_ops_operator_diagnostics_behavior",
)

MARKETPLACE_SECURITY_CORPUS_CLAIM_BOUNDARY = (
    "marketplace_registry_corpus_receipts_not_production_secure_marketplace_or_full_marketplace_parity"
)
MARKETPLACE_SECURITY_CORPUS_BLOCKED_CLAIMS = (
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


def marketplace_security_corpus_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            MARKETPLACE_SECURITY_CORPUS_SUITE_NAME,
            CONTINUOUS_VULNERABILITY_MONITORING_SUITE_NAME,
            PUBLISHER_TRUST_OPERATIONS_SUITE_NAME,
        ],
        "foundation_suites": [
            "marketplace_grade_capability_lifecycle",
            "third_party_marketplace_attestation",
            "independent_package_security_review",
            "hostile_ecosystem_package_drills",
            "package_network_incident_operations",
            "publisher_trust_vulnerability_handling",
            "marketplace_rollback_quarantine_diagnostics",
        ],
        "claim_boundary": MARKETPLACE_SECURITY_CORPUS_CLAIM_BOUNDARY,
        "receipt_surfaces": [
            "/api/operator/marketplace-security-corpus",
            "/api/operator/production-marketplace-security",
            "/api/operator/live-marketplace-attestation-proof",
            "/api/operator/marketplace-lifecycle-maturity",
            "/api/operator/benchmark-proof",
            "/api/operator/post-cq-claim-readiness",
        ],
        "corpus_policy": (
            "registry corpus receipts must expose package family, provenance, signature, publisher key, SBOM, "
            "dependency graph, compatibility, review state, scanner freshness, lifecycle diagnostics, and safe handles"
        ),
        "monitoring_policy": (
            "continuous vulnerability monitoring keeps scanner source, database freshness, waiver expiry, remediation "
            "SLA, and deny or quarantine decisions visible before promotion"
        ),
        "publisher_policy": (
            "publisher trust operations require identity, key rotation, review freshness, incident state, quarantine "
            "and re-entry decisions, and operator diagnostics across the corpus"
        ),
        "package_network_policy": (
            "package-controlled endpoints, redirects, DNS results, secret references, and workspace access deny by "
            "default unless allowlists and review receipts still match"
        ),
        "blocked_claims": list(MARKETPLACE_SECURITY_CORPUS_BLOCKED_CLAIMS),
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


def marketplace_registry_corpus_receipts() -> list[dict[str, Any]]:
    return [
        _corpus_package(
            "cx-corpus-github-reviewer",
            "marketplace.github-reviewer",
            "managed_connectors",
            "2.1.0",
            "pub.verified.neurion",
            "active",
            "allow_reviewed_install",
            "current",
            vulnerabilities=[],
        ),
        _corpus_package(
            "cx-corpus-browser-runner",
            "marketplace.browser-runner",
            "browser_providers",
            "3.4.2",
            "pub.verified.seraph-labs",
            "rotated_with_transparency_receipt",
            "hold_for_canary",
            "current_with_warning",
            vulnerabilities=[{"id": "GHSA-fixture-medium-browser-sandbox", "severity": "medium", "waiver": "current"}],
        ),
        _corpus_package(
            "cx-corpus-voice-summary",
            "marketplace.voice-summary",
            "voice_media_profiles",
            "1.8.1",
            "pub.verified.media",
            "active",
            "allow_reviewed_install",
            "current",
            vulnerabilities=[],
        ),
        _corpus_package(
            "cx-corpus-memory-adapter",
            "marketplace.memory-adapter",
            "memory_providers",
            "1.2.3",
            "pub.verified.memory",
            "active",
            "allow_reviewed_install",
            "current",
            vulnerabilities=[],
        ),
        _corpus_package(
            "cx-corpus-workflow-pack",
            "marketplace.workflow-pack",
            "workflows",
            "4.0.0",
            "pub.verified.workflow",
            "rotated_with_transparency_receipt",
            "staged_canary",
            "current",
            vulnerabilities=[{"id": "CVE-fixture-low-workflow", "severity": "low", "waiver": "not_required"}],
        ),
        _corpus_package(
            "cx-corpus-suspicious-exporter",
            "marketplace.suspicious-exporter",
            "skills",
            "0.9.5",
            "pub.unverified.unknown",
            "missing",
            "deny_and_quarantine",
            "stale_or_missing",
            signature_status="missing",
            vulnerabilities=[{"id": "CVE-fixture-critical-exporter", "severity": "critical", "waiver": "missing"}],
        ),
        _corpus_package(
            "cx-corpus-legacy-connector",
            "marketplace.legacy-connector",
            "connectors",
            "0.8.0",
            "pub.verified.legacy",
            "stale_rotation",
            "deny_until_rescan",
            "stale_review",
            vulnerabilities=[{"id": "CVE-fixture-high-stale-db", "severity": "high", "waiver": "expired"}],
        ),
        _corpus_package(
            "cx-corpus-runbook-helper",
            "marketplace.runbook-helper",
            "runbooks",
            "1.5.0",
            "pub.verified.ops",
            "active",
            "allow_reviewed_install",
            "current",
            vulnerabilities=[],
        ),
    ]


def continuous_vulnerability_monitoring_receipts() -> list[dict[str, Any]]:
    return [
        _monitor(
            "cx-monitor-osv-current",
            "osv.dev",
            "osv-scanner 1.9.1",
            "2026-06-10",
            "marketplace.github-reviewer",
            "none",
            "allow_reviewed_install",
            remediation_sla_hours=0,
        ),
        _monitor(
            "cx-monitor-nvd-current",
            "nvd.nist.gov/rest/json/cves/2.0",
            "nvd-cve-api-2.0",
            "2026-06-10",
            "marketplace.browser-runner",
            "medium_current_waiver",
            "hold_for_canary",
            waiver_expires_at="2026-06-30",
            remediation_sla_hours=168,
        ),
        _monitor(
            "cx-monitor-ghsa-current",
            "github-advisory-database",
            "ghsa-export-2026-06",
            "2026-06-10",
            "marketplace.workflow-pack",
            "low_no_waiver_required",
            "staged_canary",
            remediation_sla_hours=336,
        ),
        _monitor(
            "cx-monitor-critical-unwaived",
            "osv.dev",
            "osv-scanner 1.9.1",
            "2026-06-10",
            "marketplace.suspicious-exporter",
            "critical_unwaived",
            "deny_and_quarantine",
            remediation_sla_hours=24,
        ),
        _monitor(
            "cx-monitor-stale-db-negative",
            "scanner_fixture_stale_db",
            "fixture-stale-db-2026-03",
            "2026-03-01",
            "marketplace.legacy-connector",
            "high_expired_waiver",
            "deny_until_rescan",
            waiver_expires_at="2026-04-15",
            remediation_sla_hours=72,
        ),
    ]


def publisher_trust_operation_receipts() -> list[dict[str, Any]]:
    return [
        _publisher("cx-publisher-neurion", "pub.verified.neurion", "verified", "active", "current", "none_open", "allow_reviewed_install"),
        _publisher("cx-publisher-seraphlabs", "pub.verified.seraph-labs", "verified", "rotated", "current_with_warning", "closed_failed_update", "hold_for_canary"),
        _publisher("cx-publisher-memory", "pub.verified.memory", "verified", "active", "current", "none_open", "allow_reviewed_install"),
        _publisher("cx-publisher-legacy", "pub.verified.legacy", "verified", "stale_rotation", "stale_review", "expired_waiver", "deny_until_rescan"),
        _publisher("cx-publisher-unknown", "pub.unverified.unknown", "unverified", "missing", "stale_or_missing", "quarantine_open", "deny_and_quarantine"),
    ]


def lifecycle_operation_diagnostics() -> list[dict[str, Any]]:
    return [
        _operation("cx-op-install-reviewed", "install", "marketplace.github-reviewer", "installed_after_review", "snapshot-github-210"),
        _operation("cx-op-update-canary", "update", "marketplace.browser-runner", "staged_canary_hold", "snapshot-browser-342"),
        _operation("cx-op-downgrade-hold", "downgrade", "marketplace.voice-summary", "blocked_until_review", "snapshot-voice-181"),
        _operation("cx-op-rollback-restore", "rollback", "marketplace.browser-runner", "rolled_back", "snapshot-browser-341"),
        _operation("cx-op-quarantine-cutoff", "quarantine", "marketplace.suspicious-exporter", "runtime_cut_off", "snapshot-exporter-095"),
        _operation("cx-op-reentry-denied", "reentry_review", "marketplace.suspicious-exporter", "reentry_denied", "snapshot-exporter-095"),
    ]


def package_network_boundary_receipts() -> list[dict[str, Any]]:
    return [
        _network_boundary("cx-net-private-ssrf", "private_network_ssrf", "deny_private_network", "not_exposed", "not_requested"),
        _network_boundary("cx-net-redirect-private", "redirect_to_private_network", "deny_redirect_chain", "not_exposed", "not_requested"),
        _network_boundary("cx-net-dns-private", "dns_private_resolution", "deny_private_resolution", "not_exposed", "not_requested"),
        _network_boundary("cx-net-secret-ref", "secret_ref_injection", "deny_destination_host_mismatch", "redacted_and_rotated", "not_requested"),
        _network_boundary("cx-net-workspace-escape", "workspace_escape_attempt", "deny_workspace_escape", "not_exposed", "path_traversal_denied"),
    ]


def build_marketplace_security_corpus_contract() -> dict[str, Any]:
    corpus = marketplace_registry_corpus_receipts()
    monitors = continuous_vulnerability_monitoring_receipts()
    publishers = publisher_trust_operation_receipts()
    operations = lifecycle_operation_diagnostics()
    network = package_network_boundary_receipts()
    policy = marketplace_security_corpus_policy_payload()
    deny_actions = {"deny_and_quarantine", "deny_until_rescan"}
    return {
        "summary": {
            "operator_status": "marketplace_security_corpus_receipts_visible",
            "corpus_package_count": len(corpus),
            "package_family_count": len({item["family"] for item in corpus}),
            "continuous_monitor_count": len(monitors),
            "scanner_source_count": len({item["source"] for item in monitors}),
            "publisher_operation_count": len(publishers),
            "lifecycle_operation_count": len(operations),
            "package_network_boundary_count": len(network),
            "signature_verified_count": sum(1 for item in corpus if item["signature_status"] == "verified"),
            "sbom_dependency_digest_count": sum(
                1 for item in corpus if item.get("sbom_digest") and item.get("dependency_graph_digest")
            ),
            "review_state_visible_count": sum(1 for item in corpus if item.get("review_state")),
            "fresh_monitor_count": sum(1 for item in monitors if item["database_freshness_at"] == "2026-06-10"),
            "critical_or_high_denied_count": sum(
                1 for item in corpus
                if any(finding["severity"] in {"critical", "high"} for finding in item["vulnerabilities"])
                and item["operator_action"] in deny_actions
            ),
            "expired_waiver_denied_count": sum(
                1 for item in monitors
                if item.get("waiver_state") == "expired" and item["operator_action"] in deny_actions
            ),
            "quarantine_or_reentry_diagnostic_count": sum(
                1 for item in operations if item["operation"] in {"quarantine", "reentry_review"}
            ),
            "package_network_denial_count": sum(1 for item in network if item["decision"].startswith("deny")),
            "safe_receipts_redacted": _all_safe_receipts_redacted([*corpus, *monitors, *publishers, *operations, *network]),
            "production_secure_marketplace_claim_allowed": False,
            "claim_boundary": MARKETPLACE_SECURITY_CORPUS_CLAIM_BOUNDARY,
        },
        "registry_corpus": corpus,
        "continuous_monitoring": monitors,
        "publisher_trust_operations": publishers,
        "lifecycle_diagnostics": operations,
        "package_network_boundaries": network,
        "policy": policy,
    }


async def _run_marketplace_security_corpus_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        MARKETPLACE_SECURITY_CORPUS_SUITE_NAME,
        CONTINUOUS_VULNERABILITY_MONITORING_SUITE_NAME,
        PUBLISHER_TRUST_OPERATIONS_SUITE_NAME,
    ])


async def build_marketplace_security_corpus_report() -> dict[str, Any]:
    summary = await _run_marketplace_security_corpus_suites()
    contract = build_marketplace_security_corpus_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "marketplace_security_corpus_ci_gated_operator_visible"
                if healthy
                else "marketplace_security_corpus_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(MARKETPLACE_SECURITY_CORPUS_SCENARIO_NAMES)
                + len(CONTINUOUS_VULNERABILITY_MONITORING_SCENARIO_NAMES)
                + len(PUBLISHER_TRUST_OPERATIONS_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            MARKETPLACE_SECURITY_CORPUS_SUITE_NAME: list(MARKETPLACE_SECURITY_CORPUS_SCENARIO_NAMES),
            CONTINUOUS_VULNERABILITY_MONITORING_SUITE_NAME: list(
                CONTINUOUS_VULNERABILITY_MONITORING_SCENARIO_NAMES
            ),
            PUBLISHER_TRUST_OPERATIONS_SUITE_NAME: list(PUBLISHER_TRUST_OPERATIONS_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="marketplace_security_corpus"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def _corpus_package(
    receipt_id: str,
    package_id: str,
    family: str,
    package_version: str,
    publisher_id: str,
    key_state: str,
    operator_action: str,
    review_state: str,
    *,
    vulnerabilities: list[dict[str, Any]],
    signature_status: str = "verified",
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "package_id": package_id,
        "family": family,
        "package_version": package_version,
        "package_digest": f"sha256:cx-{package_id.replace('.', '-')}-{package_version}",
        "signed_digest": (
            f"sigstore-bundle-cx-{package_id.replace('.', '-')}-{package_version}"
            if signature_status == "verified"
            else "missing"
        ),
        "signature_status": signature_status,
        "publisher_id": publisher_id,
        "publisher_key_state": key_state,
        "review_state": review_state,
        "compatibility_state": "blocked" if operator_action in {"deny_and_quarantine", "deny_until_rescan"} else "compatible",
        "sbom_digest": f"sha256:cx-sbom-{package_id.replace('.', '-')}-{package_version}",
        "dependency_graph_digest": f"sha256:cx-deps-{package_id.replace('.', '-')}-{package_version}",
        "vulnerability_sources": ["osv.dev", "nvd.nist.gov/rest/json/cves/2.0", "github-advisory-database"],
        "database_freshness_at": "2026-06-10" if review_state.startswith("current") else "2026-03-01",
        "vulnerabilities": vulnerabilities,
        "operator_action": operator_action,
        "review_required_before_promotion": operator_action != "allow_reviewed_install",
        "package_count_claim_allowed": False,
        "safe_receipt": _safe_receipt(f"operator-cx:corpus:{package_id}"),
    }


def _monitor(
    monitor_id: str,
    source: str,
    scanner: str,
    database_freshness_at: str,
    package_id: str,
    finding_state: str,
    operator_action: str,
    *,
    waiver_expires_at: str | None = None,
    remediation_sla_hours: int,
) -> dict[str, Any]:
    waiver_state = "none"
    if waiver_expires_at:
        waiver_state = "current" if waiver_expires_at >= "2026-06-10" else "expired"
    return {
        "monitor_id": monitor_id,
        "source": source,
        "scanner": scanner,
        "database_freshness_at": database_freshness_at,
        "package_id": package_id,
        "finding_state": finding_state,
        "waiver_expires_at": waiver_expires_at,
        "waiver_state": waiver_state,
        "remediation_sla_hours": remediation_sla_hours,
        "operator_action": operator_action,
        "promotion_blocked": operator_action in {"deny_and_quarantine", "deny_until_rescan"},
        "safe_receipt": _safe_receipt(f"operator-cx:monitor:{monitor_id}"),
    }


def _publisher(
    receipt_id: str,
    publisher_id: str,
    identity_status: str,
    key_rotation_state: str,
    review_state: str,
    incident_state: str,
    operator_action: str,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "publisher_id": publisher_id,
        "identity_status": identity_status,
        "key_rotation_state": key_rotation_state,
        "review_state": review_state,
        "incident_state": incident_state,
        "operator_action": operator_action,
        "fresh_review_required": review_state not in {"current", "current_with_warning"},
        "quarantine_or_hold": operator_action in {"deny_and_quarantine", "deny_until_rescan", "hold_for_canary"},
        "safe_receipt": _safe_receipt(f"operator-cx:publisher:{publisher_id}"),
    }


def _operation(
    receipt_id: str,
    operation: str,
    package_id: str,
    state: str,
    rollback_snapshot_id: str,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "operation": operation,
        "package_id": package_id,
        "state": state,
        "rollback_snapshot_id": rollback_snapshot_id,
        "diagnostics_visible": True,
        "recovery_receipt_visible": True,
        "operator_notification_id": f"operator:marketplace-cx:operation:{operation}:{package_id}",
        "safe_receipt": _safe_receipt(f"operator-cx:operation:{receipt_id}"),
    }


def _network_boundary(
    receipt_id: str,
    boundary_class: str,
    decision: str,
    secret_ref_policy: str,
    workspace_egress_decision: str,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "boundary_class": boundary_class,
        "decision": decision,
        "secret_ref_policy": secret_ref_policy,
        "workspace_egress_decision": workspace_egress_decision,
        "audit_visible": True,
        "safe_receipt": _safe_receipt(f"operator-cx:network:{boundary_class}"),
    }


def _safe_receipt(handle: str) -> dict[str, Any]:
    return {
        "operator_receipt_handle": handle,
        "contains_secret": False,
        "contains_private_path": False,
        "raw_receipt_path_exposed": False,
        "workspace_dir_exposed": False,
        "package_path_exposed": False,
        "redaction": "metadata_only_receipt_handle",
        "redaction_layer": "marketplace_security_corpus_v1",
    }


def _all_safe_receipts_redacted(items: list[dict[str, Any]]) -> bool:
    return all(
        item["safe_receipt"]["contains_secret"] is False
        and item["safe_receipt"]["contains_private_path"] is False
        and item["safe_receipt"]["raw_receipt_path_exposed"] is False
        and item["safe_receipt"]["workspace_dir_exposed"] is False
        and item["safe_receipt"]["package_path_exposed"] is False
        and item["safe_receipt"]["redaction_layer"] == "marketplace_security_corpus_v1"
        for item in items
    )


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Marketplace security corpus scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]
