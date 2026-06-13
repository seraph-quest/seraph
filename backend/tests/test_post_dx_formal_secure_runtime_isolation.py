"""Tests for Batch DZ post-DX secure runtime isolation receipts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.security.post_dx_formal_secure_runtime_isolation import (
    CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SCENARIO_NAMES,
    CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SUITE_NAME,
    EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SCENARIO_NAMES,
    EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SUITE_NAME,
    HOSTILE_CHAIN_CONTAINMENT_V4_SCENARIO_NAMES,
    HOSTILE_CHAIN_CONTAINMENT_V4_SUITE_NAME,
    POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_BLOCKED_CLAIMS,
    POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY,
    POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SCENARIO_NAMES,
    POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SUITE_NAME,
    RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SCENARIO_NAMES,
    RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SUITE_NAME,
    SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES,
    SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SUITE_NAME,
    SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SCENARIO_NAMES,
    SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SUITE_NAME,
    build_post_dx_formal_secure_runtime_contract,
    build_post_dx_formal_secure_runtime_report,
)


def test_post_dx_formal_secure_runtime_contract_covers_dz_acceptance_fields():
    contract = build_post_dx_formal_secure_runtime_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "post_dx_formal_secure_runtime_isolation_visible"
    assert summary["claim_boundary"] == POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_CLAIM_BOUNDARY
    assert summary["implemented_attestation_count"] >= 5
    assert summary["unsupported_boundary_marker_count"] >= 2
    assert summary["all_attestations_have_provenance"] is True
    assert summary["credential_egress_block_count"] >= 5
    assert summary["credential_leak_count"] == 0
    assert summary["hostile_chain_fail_closed_count"] == summary["hostile_chain_count"]
    assert summary["hostile_chain_quarantine_count"] == summary["hostile_chain_count"]
    assert summary["formal_certification_granted_count"] == 0
    assert summary["waiver_record_count"] >= 1
    assert summary["operator_owned_recovery_count"] == summary["operator_recovery_action_count"]
    assert summary["automatic_authority_expansion_count"] == 0
    assert summary["all_false_claim_scans_command_backed"] is True
    assert summary["all_gate_checks_passed"] is True
    assert all(contract["gate_checks"].values())
    assert set(POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/post-dx-formal-secure-runtime-isolation" in policy["receipt_surfaces"]


def test_post_dx_formal_secure_runtime_redacts_and_blocks_overclaims():
    contract = build_post_dx_formal_secure_runtime_contract()
    serialized = json.dumps(contract, sort_keys=True)
    attestation = contract["runtime_attestation_evidence_v2"]
    reviews = contract["external_security_review_certification_track_v2"]

    assert "Authorization: Bearer" not in serialized
    assert "sk-live" not in serialized.lower()
    assert "prod-secure-token" not in serialized
    assert "ironclaw_class_secure_execution" in serialized
    assert any(item["surface"] == "hardware_backed_runtime" and item["implemented"] is False for item in attestation)
    assert any("tee_cvm_wasm" in item["surface"] and item["implemented"] is False for item in attestation)
    assert all(item["runtime_fetch_performed"] is False for item in attestation)
    assert all(item["redacted_receipt_handle"].startswith("seraph://receipts/batch-dz/") for item in attestation)
    assert all(item["formal_certification_granted"] is False for item in reviews)
    assert all(item["artifact_digest"].startswith("sha256:") for item in reviews)


def test_post_dx_formal_secure_runtime_report_runs_all_dz_suites():
    scenario_names = [
        *POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SCENARIO_NAMES,
        *RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SCENARIO_NAMES,
        *CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SCENARIO_NAMES,
        *HOSTILE_CHAIN_CONTAINMENT_V4_SCENARIO_NAMES,
        *EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SCENARIO_NAMES,
        *SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SCENARIO_NAMES,
        *SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES,
    ]
    summary = SimpleNamespace(
        total=len(scenario_names),
        passed=len(scenario_names),
        failed=0,
        duration_ms=44,
        results=[SimpleNamespace(name=name, passed=True, error="") for name in scenario_names],
    )

    with patch(
        "src.security.post_dx_formal_secure_runtime_isolation._run_post_dx_formal_secure_runtime_suites",
        AsyncMock(return_value=summary),
    ):
        report = asyncio.run(build_post_dx_formal_secure_runtime_report())

    assert report["summary"]["benchmark_posture"] == "bounded_post_dx_formal_secure_runtime_isolation_proof"
    assert report["summary"]["scenario_count"] == len(scenario_names)
    assert report["latest_run"]["failed"] == 0
    assert report["failure_report"] == []
    assert report["scenario_names"][POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SUITE_NAME] == list(
        POST_DX_FORMAL_SECURE_RUNTIME_ISOLATION_SCENARIO_NAMES
    )
    assert report["scenario_names"][RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SUITE_NAME] == list(
        RUNTIME_ISOLATION_ATTESTATION_EVIDENCE_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SUITE_NAME] == list(
        CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_V3_SCENARIO_NAMES
    )
    assert report["scenario_names"][HOSTILE_CHAIN_CONTAINMENT_V4_SUITE_NAME] == list(
        HOSTILE_CHAIN_CONTAINMENT_V4_SCENARIO_NAMES
    )
    assert report["scenario_names"][EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SUITE_NAME] == list(
        EXTERNAL_SECURITY_REVIEW_CERTIFICATION_TRACK_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SUITE_NAME] == list(
        SECURE_RUNTIME_RECOVERY_AUTHORITY_V3_SCENARIO_NAMES
    )
    assert report["scenario_names"][SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SUITE_NAME] == list(
        SECURE_RUNTIME_FALSE_CLAIM_SCAN_V3_SCENARIO_NAMES
    )
