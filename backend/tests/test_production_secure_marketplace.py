import asyncio
import json

from src.extensions.production_secure_marketplace import (
    HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SCENARIO_NAMES,
    MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SCENARIO_NAMES,
    PRODUCTION_SECURE_MARKETPLACE_BLOCKED_CLAIMS,
    PRODUCTION_SECURE_MARKETPLACE_CLAIM_BOUNDARY,
    PRODUCTION_SECURE_MARKETPLACE_V1_SCENARIO_NAMES,
    REQUIRED_HOSTILE_PACKAGE_DRILLS,
    REQUIRED_MARKETPLACE_LIFECYCLE_FLOWS,
    THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SCENARIO_NAMES,
    build_production_secure_marketplace_contract,
    build_production_secure_marketplace_report,
)


def test_production_secure_marketplace_contract_exposes_df_boundary_and_gates():
    contract = build_production_secure_marketplace_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "production_secure_marketplace_receipts_visible"
    assert summary["claim_boundary"] == PRODUCTION_SECURE_MARKETPLACE_CLAIM_BOUNDARY
    assert summary["production_gate_count"] >= 5
    assert summary["certification_review_count"] >= 4
    assert summary["live_corpus_package_count"] >= 12
    assert summary["live_corpus_family_count"] >= 10
    assert summary["lifecycle_flow_count"] >= len(REQUIRED_MARKETPLACE_LIFECYCLE_FLOWS)
    assert summary["hostile_gauntlet_count"] >= len(REQUIRED_HOSTILE_PACKAGE_DRILLS)
    assert summary["production_secure_marketplace_claim_allowed"] is False
    assert summary["third_party_package_security_solved_claim_allowed"] is False
    assert set(PRODUCTION_SECURE_MARKETPLACE_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/production-secure-marketplace" in policy["receipt_surfaces"]


def test_production_secure_marketplace_live_corpus_has_quality_receipts_and_blocks_risky_packages():
    contract = build_production_secure_marketplace_contract()
    summary = contract["summary"]
    corpus = {item["package_id"]: item for item in contract["live_corpus_operations_v2"]}

    assert summary["promoted_package_proof_complete"] is True
    assert summary["blocked_or_held_risky_package_count"] >= 3
    assert summary["critical_high_denied_count"] >= 2
    assert all(item["package_digest"].startswith("sha256:df-") for item in corpus.values())
    assert all(item["sbom_digest"].startswith("sha256:df-sbom-") for item in corpus.values())
    assert all(item["dependency_graph_digest"].startswith("sha256:df-deps-") for item in corpus.values())
    promoted = [
        item for item in corpus.values()
        if item["promotion_decision"] in {"allow_promote", "staged_rollout"}
    ]
    assert all(item["publisher_identity_verified"] is True for item in promoted)
    assert all(item["publisher_key_state"] == "active" for item in promoted)
    assert all(item["compatibility_state"] == "compatible" for item in promoted)
    assert all(item["scanner_freshness_at"] == "2026-06-11" for item in promoted)
    assert all(item["quarantine_state"] == "not_quarantined" for item in promoted)
    assert all(item["reentry_required"] is False for item in promoted)
    assert corpus["marketplace.suspicious-exporter"]["promotion_decision"] == "deny_and_quarantine"
    assert corpus["marketplace.suspicious-exporter"]["signature_status"] == "missing"
    assert corpus["marketplace.legacy-connector"]["promotion_decision"] == "deny_until_rescan"
    assert corpus["marketplace.mcp-bridge"]["promotion_decision"] == "hold_for_external_retest"
    assert all(item["package_count_claim_allowed"] is False for item in corpus.values())


def test_package_security_certification_receipts_have_scope_retests_and_claim_blocks():
    contract = build_production_secure_marketplace_contract()
    summary = contract["summary"]
    certifications = contract["third_party_package_security_certification"]

    assert summary["external_review_scope_count"] >= 10
    assert summary["certification_findings_retested"] is True
    assert summary["certification_review_proofs_bound"] is True
    assert summary["certification_claim_lift_blocked"] is True
    assert summary["waiver_expiry_visible"] is True
    assert all(item["reviewer_independence"] for item in certifications)
    assert all(item["reviewer_identity_verified"] is True for item in certifications)
    assert all(item["reviewer_conflict_checked"] is True for item in certifications)
    assert all(item["publisher_conflict_detected"] is False for item in certifications)
    assert all(item["review_report_digest"] for item in certifications)
    assert all(item["scope_artifact_digest"] for item in certifications)
    assert all(item["signed_reviewer_receipt"] for item in certifications)
    assert all(
        item["review_evidence_mode"] == "fixture_external_review_receipt_not_formal_certification"
        for item in certifications
    )
    assert all(item["all_findings_retested"] is True for item in certifications)
    assert all(item["claim_lift_allowed"] is False for item in certifications)
    assert all(item["open_unwaived_critical_count"] == 0 for item in certifications)


def test_hostile_gauntlet_and_lifecycle_flows_fail_closed_and_notify_operator():
    contract = build_production_secure_marketplace_contract()
    summary = contract["summary"]
    flows = {item["flow"]: item for item in contract["lifecycle_flow_receipts"]}
    hostile = {item["drill_class"]: item for item in contract["hostile_package_lifecycle_gauntlet"]}

    assert summary["required_lifecycle_flows_covered"] is True
    assert summary["required_hostile_drills_covered"] is True
    assert summary["hostile_gauntlet_fail_closed"] is True
    assert set(REQUIRED_MARKETPLACE_LIFECYCLE_FLOWS) <= set(flows)
    assert set(REQUIRED_HOSTILE_PACKAGE_DRILLS) <= set(hostile)
    assert flows["rollback"]["state"] == "rolled_back"
    assert flows["quarantine"]["state"] == "quarantined_runtime_cutoff"
    assert hostile["secret_exfiltration"]["decision"] == "deny_secret_destination_mismatch"
    assert hostile["workspace_access"]["decision"] == "deny_workspace_escape"
    assert hostile["malicious_update"]["decision"] == "deny_update_and_restore_previous"
    assert hostile["install_script_execution"]["decision"] == "deny_install_hook_execution"
    assert hostile["typosquatting"]["decision"] == "deny_confusable_package_name"
    assert hostile["transitive_dependency_compromise"]["decision"] == "deny_transitive_digest_mismatch"
    assert hostile["compromised_signing_key"]["decision"] == "deny_revoked_signing_key"
    assert hostile["native_binary_artifact"]["decision"] == "deny_unreviewed_native_artifact"
    assert hostile["archive_path_traversal"]["decision"] == "deny_archive_path_traversal"
    assert hostile["symlink_escape"]["decision"] == "deny_symlink_workspace_escape"
    assert hostile["runtime_fetch"]["decision"] == "deny_unapproved_runtime_fetch"
    assert hostile["dynamic_import"]["decision"] == "deny_unreviewed_dynamic_import"
    assert hostile["package_prompt_injection"]["decision"] == "deny_package_prompt_instruction"
    assert hostile["tool_injection"]["decision"] == "deny_tool_schema_mutation"
    assert all(item["runtime_contribution_allowed"] is False for item in hostile.values())
    assert summary["operator_notification_count"] >= len(flows) + len(hostile)


def test_production_secure_marketplace_receipts_are_redacted():
    contract = build_production_secure_marketplace_contract()
    encoded = json.dumps(contract, sort_keys=True)

    assert contract["summary"]["safe_receipts_redacted"] is True
    assert "/Users/" not in encoded
    assert "file://" not in encoded
    assert ".env" not in encoded
    assert "id_rsa" not in encoded
    assert "sk-" not in encoded
    assert "raw_receipt_location" not in encoded
    for group in (
        contract["production_gates"],
        contract["third_party_package_security_certification"],
        contract["live_corpus_operations_v2"],
        contract["lifecycle_flow_receipts"],
        contract["hostile_package_lifecycle_gauntlet"],
    ):
        for item in group:
            receipt = item["safe_receipt"]
            assert receipt["contains_secret"] is False
            assert receipt["contains_private_path"] is False
            assert receipt["contains_raw_package_path"] is False
            assert receipt["raw_receipt_path_exposed"] is False
            assert receipt["workspace_dir_exposed"] is False
            assert receipt["package_path_exposed"] is False
            assert receipt["redaction_layer"] == "production_secure_marketplace_v1"
            assert len(receipt["evidence_body_digest"]) == 64
            assert receipt["sanitized_payload_digest"] == receipt["evidence_body_digest"]
            assert len(receipt["tamper_evident_digest"]) == 64
            assert receipt["tamper_evident_digest"] != receipt["evidence_body_digest"]


def test_production_secure_marketplace_report_runs_all_batch_df_suites():
    payload = asyncio.run(build_production_secure_marketplace_report())

    assert payload["summary"]["benchmark_posture"] == "production_secure_marketplace_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(PRODUCTION_SECURE_MARKETPLACE_V1_SCENARIO_NAMES)
        + len(THIRD_PARTY_PACKAGE_SECURITY_CERTIFICATION_V1_SCENARIO_NAMES)
        + len(MARKETPLACE_LIVE_CORPUS_OPERATIONS_V2_SCENARIO_NAMES)
        + len(HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V1_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == PRODUCTION_SECURE_MARKETPLACE_CLAIM_BOUNDARY
