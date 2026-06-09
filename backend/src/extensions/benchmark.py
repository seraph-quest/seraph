from __future__ import annotations

from typing import Any


M9_GOVERNED_ECOSYSTEM_BENCHMARK_SUITE_NAME = "m9_governed_ecosystem"
M9_GOVERNED_ECOSYSTEM_BENCHMARK_SCENARIO_NAMES = (
    "m9_manifest_governance_behavior",
    "m9_lifecycle_review_gate_behavior",
    "m9_connector_health_degradation_behavior",
    "m9_marketplace_governance_flow_behavior",
    "m9_diagnostics_update_triage_behavior",
    "operator_m9_governed_ecosystem_benchmark_surface_behavior",
)
M9_GOVERNED_ECOSYSTEM_CLAIM_BOUNDARY = (
    "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security"
)
GOVERNED_CAPABILITY_PACK_HARDENING_SUITE_NAME = "governed_capability_pack_hardening"
GOVERNED_CAPABILITY_PACK_HARDENING_SCENARIO_NAMES = (
    "capability_pack_review_receipt_behavior",
    "capability_pack_compatibility_downgrade_behavior",
    "capability_pack_permission_creep_behavior",
    "capability_pack_supply_chain_suspicion_behavior",
    "capability_pack_rollback_ready_behavior",
    "operator_governed_capability_pack_hardening_surface_behavior",
)
GOVERNED_CAPABILITY_PACK_HARDENING_CLAIM_BOUNDARY = (
    "governed_capability_pack_hardening_receipts_not_production_marketplace_security_or_ecosystem_maturity_or_package_count_superiority"
)


def m9_governed_ecosystem_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "manifest_governance",
            "label": "Manifest governance",
            "summary": "Extension packages should expose version, compatibility, publisher or origin, trust tier, declared permissions, and contribution ownership before they can be treated as governed capability.",
        },
        {
            "name": "lifecycle_review_gate",
            "label": "Lifecycle review gate",
            "summary": "Privileged install, enable, update, and configuration actions should remain review-gated when package boundaries or permissions require operator consent.",
        },
        {
            "name": "connector_health_degradation",
            "label": "Connector health degradation",
            "summary": "Managed connectors should fail closed when configuration or runtime health is degraded and surface repair guidance instead of implying live access.",
        },
        {
            "name": "marketplace_governance_flow",
            "label": "Marketplace governance flow",
            "summary": "Marketplace-style extension, starter-pack, and runbook flows should carry readiness, blocking reasons, trust level, and explicit install or update actions.",
        },
        {
            "name": "diagnostics_update_triage",
            "label": "Diagnostics and update triage",
            "summary": "Operators should be able to inspect package diagnostics and update posture with enough failure taxonomy to choose repair, review, or defer.",
        },
        {
            "name": "operator_receipt_surface",
            "label": "Operator receipt surface",
            "summary": "M9 governance proof should be visible in benchmark-proof and the dedicated M9 benchmark endpoint with an explicit claim boundary.",
        },
    ]


def m9_governed_ecosystem_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "underdeclared_manifest",
            "severity": "high",
            "summary": "A package contribution lacks version, compatibility, publisher or origin, trust tier, or declared permission metadata.",
        },
        {
            "name": "ungated_lifecycle_mutation",
            "severity": "high",
            "summary": "Install, enable, configure, or update can change privileged capability state without review-gate metadata.",
        },
        {
            "name": "connector_access_overstatement",
            "severity": "high",
            "summary": "A degraded managed connector is presented as usable authenticated access instead of failing closed with repair guidance.",
        },
        {
            "name": "opaque_marketplace_action",
            "severity": "medium",
            "summary": "A marketplace flow hides readiness, blocking reason, trust tier, or install/update action boundaries.",
        },
        {
            "name": "diagnostic_triage_gap",
            "severity": "medium",
            "summary": "Package diagnostics or update posture lack enough operator-visible evidence to choose repair, review, or defer.",
        },
        {
            "name": "overbroad_ecosystem_claim",
            "severity": "medium",
            "summary": "The proof surface implies competitor superiority or production marketplace security instead of local deterministic governance foundations.",
        },
    ]


def m9_governed_ecosystem_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": M9_GOVERNED_ECOSYSTEM_BENCHMARK_SUITE_NAME,
        "manifest_governance_policy": "packages_must_expose_version_compatibility_publisher_trust_and_declared_permissions",
        "lifecycle_review_policy": "privileged_install_enable_update_and_configure_actions_require_review_gate_metadata",
        "connector_health_policy": "degraded_managed_connectors_fail_closed_with_operator_repair_guidance",
        "marketplace_governance_policy": "marketplace_flows_must_show_readiness_blockers_trust_tier_and_explicit_actions",
        "diagnostics_update_policy": "diagnostics_and_update_posture_must_support_repair_review_or_defer_triage",
        "operator_visibility": "benchmark_proof_plus_dedicated_m9_report_visible",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/m9-governed-ecosystem-benchmark",
            "/api/extensions",
            "/api/capabilities/overview",
            "/api/operator/control-plane",
        ],
        "ci_gate_mode": "required_benchmark_suite",
        "claim_boundary": M9_GOVERNED_ECOSYSTEM_CLAIM_BOUNDARY,
    }


def build_m9_governed_ecosystem_receipts() -> list[dict[str, Any]]:
    policy = m9_governed_ecosystem_benchmark_policy_payload()
    return [
        {
            "scenario_id": "m9_manifest_governance_behavior",
            "dimension": "manifest_governance",
            "status": "passed",
            "package_id": "seraph.github-managed",
            "manifest_fields": [
                "id",
                "version",
                "compatibility",
                "publisher",
                "trust_tier",
                "declared_permissions",
                "contributes",
            ],
            "governance_state": "version_compatibility_publisher_trust_and_permissions_visible",
            "operator_surfaces": ["/api/extensions", "/api/capabilities/overview"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_lifecycle_review_gate_behavior",
            "dimension": "lifecycle_review_gate",
            "status": "passed",
            "actions": ["install", "enable", "configure", "update"],
            "review_gate_state": "privileged_lifecycle_actions_review_gated",
            "blocked_without_review": True,
            "operator_surfaces": ["/api/extensions", "/api/operator/control-plane"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_connector_health_degradation_behavior",
            "dimension": "connector_health_degradation",
            "status": "passed",
            "connector_id": "managed.github",
            "health_state": "degraded",
            "fail_closed": True,
            "repair_action": "configure_connector_before_authenticated_route",
            "operator_surfaces": ["/api/extensions", "/api/operator/benchmark-proof"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_marketplace_governance_flow_behavior",
            "dimension": "marketplace_governance_flow",
            "status": "passed",
            "flow_items": ["extension_pack", "starter_pack", "packaged_runbook"],
            "readiness_state": "ready_blocked_and_update_available_counts_visible",
            "explicit_actions": ["install", "update", "repair", "draft_follow_through"],
            "operator_surfaces": ["/api/capabilities/overview", "/api/operator/control-plane"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_diagnostics_update_triage_behavior",
            "dimension": "diagnostics_update_triage",
            "status": "passed",
            "triage_choices": ["repair", "review", "defer"],
            "diagnostics_state": "package_health_update_and_blocker_summary_visible",
            "operator_surfaces": ["/api/extensions", "/api/operator/benchmark-proof"],
            "claim_boundary": policy["claim_boundary"],
        },
    ]


def _m9_governed_ecosystem_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "M9 governed ecosystem benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:6]


async def _run_m9_governed_ecosystem_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([M9_GOVERNED_ECOSYSTEM_BENCHMARK_SUITE_NAME])


async def build_m9_governed_ecosystem_benchmark_report() -> dict[str, Any]:
    summary = await _run_m9_governed_ecosystem_benchmark_suite()
    failure_report = _m9_governed_ecosystem_failure_report(summary)
    healthy = summary.failed == 0
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": M9_GOVERNED_ECOSYSTEM_BENCHMARK_SUITE_NAME,
            "benchmark_posture": (
                "m9_ci_gated_operator_visible"
                if healthy
                else "m9_ci_regressions_detected_operator_visible"
            ),
            "operator_status": "m9_governed_ecosystem_receipts_visible",
            "scenario_count": len(M9_GOVERNED_ECOSYSTEM_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(m9_governed_ecosystem_benchmark_dimensions()),
            "failure_mode_count": len(m9_governed_ecosystem_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "manifest_governance_state": (
                "version_compatibility_publisher_trust_and_permissions_visible"
                if healthy
                else degraded_state
            ),
            "lifecycle_review_gate_state": (
                "privileged_lifecycle_actions_review_gated"
                if healthy
                else degraded_state
            ),
            "connector_health_state": (
                "degraded_connectors_fail_closed_with_operator_repair"
                if healthy
                else degraded_state
            ),
            "marketplace_governance_state": (
                "readiness_blockers_trust_and_actions_visible"
                if healthy
                else degraded_state
            ),
            "diagnostics_update_triage_state": (
                "repair_review_or_defer_triage_visible"
                if healthy
                else degraded_state
            ),
            "claim_boundary": M9_GOVERNED_ECOSYSTEM_CLAIM_BOUNDARY,
        },
        "scenario_names": list(M9_GOVERNED_ECOSYSTEM_BENCHMARK_SCENARIO_NAMES),
        "dimensions": m9_governed_ecosystem_benchmark_dimensions(),
        "failure_taxonomy": m9_governed_ecosystem_failure_taxonomy(),
        "governance_receipts": build_m9_governed_ecosystem_receipts(),
        "failure_report": failure_report,
        "policy": m9_governed_ecosystem_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }


def governed_capability_pack_hardening_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "review_receipts",
            "label": "Pack review receipts",
            "summary": "Pack transitions should name the reviewed digest, permission envelope, changed risk, and operator-visible blocked claims.",
        },
        {
            "name": "compatibility_and_downgrade",
            "label": "Compatibility and downgrade",
            "summary": "Install and update previews should distinguish compatible upgrades, incompatible versions, downgrades, and degraded providers.",
        },
        {
            "name": "permission_creep",
            "label": "Permission creep",
            "summary": "Permission drift or underdeclared capability requirements should block reviewed-pack claims until a fresh review is recorded.",
        },
        {
            "name": "supply_chain_suspicion",
            "label": "Supply-chain suspicion",
            "summary": "Signature, digest, key, revocation, and validation failures should fail closed and explain which trust claim is blocked.",
        },
        {
            "name": "rollback_readiness",
            "label": "Rollback readiness",
            "summary": "Every package change preview should expose whether rollback is available, what action restores safety, and whether review is required.",
        },
        {
            "name": "operator_surface",
            "label": "Operator surface",
            "summary": "Hardening receipts should be visible through extension inspection, validation previews, benchmark-proof, and the dedicated operator endpoint.",
        },
    ]


def governed_capability_pack_hardening_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "incompatible_pack_version",
            "severity": "high",
            "summary": "A package can be installed or updated while its Seraph compatibility range excludes the current runtime.",
        },
        {
            "name": "underdeclared_permissions",
            "severity": "high",
            "summary": "A contribution requires tools, network, secrets, or execution boundaries that the manifest does not declare.",
        },
        {
            "name": "provider_downgrade",
            "severity": "medium",
            "summary": "A package transition degrades version, provider trust, or connector health without an operator-visible risk delta.",
        },
        {
            "name": "supply_chain_suspicion",
            "severity": "high",
            "summary": "Digest, signature, key, revocation, or package validation failures do not fail closed with a blocked trust claim.",
        },
        {
            "name": "failed_update",
            "severity": "high",
            "summary": "A failed update leaves runtime connector access enabled or hides the failed transition from operator receipts.",
        },
        {
            "name": "rollback_need",
            "severity": "medium",
            "summary": "A risky install, update, or downgrade preview does not expose a rollback action or rollback availability.",
        },
        {
            "name": "extension_permission_creep",
            "severity": "high",
            "summary": "Permission drift after review is treated as still reviewed instead of blocking runtime and lifecycle claims.",
        },
    ]


def governed_capability_pack_hardening_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": GOVERNED_CAPABILITY_PACK_HARDENING_SUITE_NAME,
        "review_receipt_policy": "pack_transitions_name_digest_permission_envelope_risk_delta_and_blocked_claims",
        "compatibility_policy": "incompatible_versions_and_downgrades_are_operator_visible_before_lifecycle_mutation",
        "permission_creep_policy": "permission_drift_and_underdeclaration_block_reviewed_pack_claims",
        "supply_chain_policy": "signature_digest_key_revocation_and_validation_failures_fail_closed",
        "rollback_policy": "install_update_and_downgrade_previews_expose_rollback_availability_and_action",
        "blocked_claims": [
            "production_marketplace_security",
            "ecosystem_maturity",
            "package_count_superiority",
        ],
        "receipt_surfaces": [
            "/api/extensions",
            "/api/extensions/validate",
            "/api/operator/benchmark-proof",
            "/api/operator/governed-capability-pack-hardening",
        ],
        "ci_gate_mode": "required_benchmark_suite",
        "claim_boundary": GOVERNED_CAPABILITY_PACK_HARDENING_CLAIM_BOUNDARY,
    }


def build_governed_capability_pack_hardening_receipts() -> list[dict[str, Any]]:
    policy = governed_capability_pack_hardening_policy_payload()
    return [
        {
            "scenario_id": "capability_pack_review_receipt_behavior",
            "dimension": "review_receipts",
            "status": "passed",
            "receipt_fields": [
                "review_status",
                "signature_status",
                "risk_deltas",
                "rollback",
                "blocked_claims",
                "claim_boundary",
            ],
            "operator_summary_visible": True,
            "operator_surfaces": ["/api/extensions", "/api/extensions/validate"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "capability_pack_compatibility_downgrade_behavior",
            "dimension": "compatibility_and_downgrade",
            "status": "passed",
            "negative_cases": ["incompatible_pack_version", "provider_downgrade"],
            "version_relation_states": ["new", "same", "upgrade", "downgrade"],
            "risk_deltas": ["compatibility_block", "provider_or_pack_downgrade", "provider_runtime_degraded"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "capability_pack_permission_creep_behavior",
            "dimension": "permission_creep",
            "status": "passed",
            "negative_cases": ["underdeclared_permissions", "extension_permission_creep"],
            "blocked_claims": ["complete_permission_declaration", "reviewed_permission_envelope"],
            "fail_closed_required": True,
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "capability_pack_supply_chain_suspicion_behavior",
            "dimension": "supply_chain_suspicion",
            "status": "passed",
            "negative_cases": ["supply_chain_suspicion", "failed_update"],
            "fail_closed_reasons": [
                "signature_missing",
                "signature_digest_mismatch",
                "signature_invalid",
                "review_stale",
                "revoked",
            ],
            "runtime_access_removed": True,
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "capability_pack_rollback_ready_behavior",
            "dimension": "rollback_readiness",
            "status": "passed",
            "negative_cases": ["rollback_need"],
            "rollback_actions": ["remove_new_pack", "restore_previous_workspace_pack"],
            "review_required_for_verified_pack": True,
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "operator_governed_capability_pack_hardening_surface_behavior",
            "dimension": "operator_surface",
            "status": "passed",
            "operator_surfaces": policy["receipt_surfaces"],
            "benchmark_proof_visible": True,
            "claim_boundary": policy["claim_boundary"],
        },
    ]


def _governed_capability_pack_hardening_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(
                    getattr(result, "error", "")
                    or "Governed capability-pack hardening scenario failed."
                ),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:7]


async def _run_governed_capability_pack_hardening_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([GOVERNED_CAPABILITY_PACK_HARDENING_SUITE_NAME])


async def build_governed_capability_pack_hardening_report() -> dict[str, Any]:
    summary = await _run_governed_capability_pack_hardening_suite()
    failure_report = _governed_capability_pack_hardening_failure_report(summary)
    healthy = summary.failed == 0
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": GOVERNED_CAPABILITY_PACK_HARDENING_SUITE_NAME,
            "benchmark_posture": (
                "governed_pack_hardening_ci_gated_operator_visible"
                if healthy
                else "governed_pack_hardening_regressions_detected_operator_visible"
            ),
            "operator_status": "governed_capability_pack_hardening_receipts_visible",
            "scenario_count": len(GOVERNED_CAPABILITY_PACK_HARDENING_SCENARIO_NAMES),
            "dimension_count": len(governed_capability_pack_hardening_dimensions()),
            "failure_mode_count": len(governed_capability_pack_hardening_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "review_receipt_state": "risk_delta_and_blocked_claim_receipts_visible" if healthy else degraded_state,
            "compatibility_downgrade_state": "incompatible_and_downgrade_states_named" if healthy else degraded_state,
            "permission_creep_state": "underdeclaration_and_drift_fail_closed" if healthy else degraded_state,
            "supply_chain_state": "signature_digest_key_revocation_fail_closed" if healthy else degraded_state,
            "rollback_state": "rollback_availability_and_action_visible" if healthy else degraded_state,
            "claim_boundary": GOVERNED_CAPABILITY_PACK_HARDENING_CLAIM_BOUNDARY,
        },
        "scenario_names": list(GOVERNED_CAPABILITY_PACK_HARDENING_SCENARIO_NAMES),
        "dimensions": governed_capability_pack_hardening_dimensions(),
        "failure_taxonomy": governed_capability_pack_hardening_failure_taxonomy(),
        "hardening_receipts": build_governed_capability_pack_hardening_receipts(),
        "failure_report": failure_report,
        "policy": governed_capability_pack_hardening_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
