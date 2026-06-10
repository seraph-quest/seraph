"""Tests for Batch CA marketplace lifecycle proof receipts."""

import asyncio

from src.extensions.marketplace_lifecycle import (
    CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES,
    GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES,
    MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES,
    MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS,
    MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
    build_marketplace_lifecycle_contract,
    build_marketplace_lifecycle_report,
)


def test_marketplace_lifecycle_contract_exposes_core_counts_and_boundary():
    contract = build_marketplace_lifecycle_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "marketplace_lifecycle_maturity_receipts_visible"
    assert summary["lifecycle_action_count"] == 9
    assert summary["family_count"] == 11
    assert summary["negative_case_count"] == 5
    assert summary["staged_rollout_count"] == 2
    assert summary["cross_family_coverage_visible"] is True
    assert summary["package_count_substitution_blocked"] is True
    assert summary["claim_boundary"] == MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY
    assert set(MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/marketplace-lifecycle-maturity" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_marketplace_lifecycle_receipts_include_deltas_and_recovery():
    contract = build_marketplace_lifecycle_contract()
    lifecycle = contract["lifecycle_receipts"]
    actions = {item["action"]: item for item in lifecycle}

    assert set(actions) == {
        "install",
        "update",
        "downgrade",
        "disable",
        "rollback",
        "review",
        "quarantine",
        "diagnostics",
        "staged_rollout",
    }
    assert actions["install"]["permission_delta"]["added"] == ["network.request"]
    assert actions["update"]["risk_delta"]["risk_after"] == "high"
    assert actions["downgrade"]["failure_recovery"]["fails_closed"] is True
    assert actions["rollback"]["rollback"]["receipt_required"] is True
    assert actions["quarantine"]["lifecycle_state"] == "fail_closed"
    assert all(item["operator_receipt_id"].startswith("operator:marketplace-ca:") for item in lifecycle)


def test_marketplace_lifecycle_negative_cases_fail_closed():
    contract = build_marketplace_lifecycle_contract()
    cases = {item["case_id"]: item for item in contract["negative_cases"]}

    assert cases["incompatible-version"]["state"] == "blocked"
    assert cases["underdeclared-permissions"]["state"] == "blocked"
    assert cases["suspicious-digest"]["state"] == "quarantined"
    assert cases["failed-update"]["state"] == "rolled_back"
    assert cases["permission-creep"]["state"] == "quarantined"
    assert all(item["fails_closed"] is True for item in cases.values())


def test_marketplace_lifecycle_family_coverage_spans_capability_surface():
    contract = build_marketplace_lifecycle_contract()
    families = {item["family"]: item for item in contract["family_coverage"]}

    assert {
        "skills",
        "workflows",
        "runbooks",
        "starter_packs",
        "connectors",
        "browser_providers",
        "messaging_connectors",
        "node_adapters",
        "memory_providers",
        "voice_media_profiles",
        "managed_connectors",
    } <= set(families)
    assert all(item["permission_delta_visible"] is True for item in families.values())
    assert all(item["rollback_visible"] is True for item in families.values())
    assert all(item["diagnostics_visible"] is True for item in families.values())


def test_marketplace_lifecycle_report_runs_all_batch_ca_suites():
    payload = asyncio.run(build_marketplace_lifecycle_report())

    assert payload["summary"]["benchmark_posture"] == "marketplace_lifecycle_maturity_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES)
        + len(GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES)
        + len(CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES)
    )
    assert payload["latest_run"]["total"] == payload["summary"]["scenario_count"]
    assert payload["latest_run"]["failed"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY
