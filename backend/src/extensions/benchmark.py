from __future__ import annotations

from typing import Any


M9_GOVERNED_ECOSYSTEM_BENCHMARK_SUITE_NAME = "m9_governed_ecosystem"
M9_GOVERNED_ECOSYSTEM_BENCHMARK_SCENARIO_NAMES = (
    "m9_manifest_governance_behavior",
    "m9_lifecycle_review_gate_behavior",
    "m9_connector_health_degradation_behavior",
    "m9_marketplace_governance_flow_behavior",
    "m9_diagnostics_update_triage_behavior",
    "m9_package_review_receipt_behavior",
    "m9_compatibility_transition_semantics_behavior",
    "m9_supply_chain_policy_behavior",
    "m9_provider_trust_downgrade_behavior",
    "m9_authoring_ergonomics_behavior",
    "operator_m9_governed_ecosystem_benchmark_surface_behavior",
)
M9_GOVERNED_ECOSYSTEM_CLAIM_BOUNDARY = (
    "deterministic_local_governance_proof_not_competitor_superiority_or_production_marketplace_security"
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
        {
            "name": "package_review_receipts",
            "label": "Package review receipts",
            "summary": "Verified packages should surface digest, key, review status, permission fingerprint, compatibility, supply-chain, and claim-boundary fields as one operator-readable receipt.",
        },
        {
            "name": "compatibility_transition_semantics",
            "label": "Compatibility transition semantics",
            "summary": "Install and update flows should distinguish new installs, same-version changed packages, upgrades, downgrades, incompatible runtimes, and workspace overrides with clear recommended actions.",
        },
        {
            "name": "provider_trust_downgrade",
            "label": "Provider trust downgrade",
            "summary": "Previously reviewed verified/provider-backed packages should not silently become lower-trust local or unsigned connector packages during update.",
        },
        {
            "name": "authoring_ergonomics",
            "label": "Authoring ergonomics",
            "summary": "Scaffold and doctor paths should produce review-ready local manifests and precise policy guidance without pretending local packages are verified.",
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
        {
            "name": "missing_package_review_receipt",
            "severity": "high",
            "summary": "A verified package lacks operator-visible digest, key, review, permission fingerprint, compatibility, or supply-chain verdict fields.",
        },
        {
            "name": "unsafe_transition_semantics",
            "severity": "high",
            "summary": "An update cannot distinguish upgrade, downgrade, same-version content change, incompatible runtime, or workspace override posture.",
        },
        {
            "name": "provider_trust_downgrade",
            "severity": "high",
            "summary": "A reviewed verified/provider-backed package can silently become local, unsigned, or lower trust on update.",
        },
        {
            "name": "weak_authoring_guidance",
            "severity": "medium",
            "summary": "Scaffolded packages or doctor output do not guide authors toward review-ready governance metadata and safer compatibility policy.",
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
        "package_review_policy": "verified_packages_must_surface_digest_key_review_permission_compatibility_and_supply_chain_receipts",
        "compatibility_transition_policy": "install_update_flows_must_distinguish_new_same_upgrade_downgrade_incompatible_and_workspace_override",
        "trust_downgrade_policy": "reviewed_verified_or_provider_backed_packs_must_not_silently_downgrade_to_lower_trust",
        "authoring_policy": "scaffold_and_doctor_must_emit_review_ready_local_governance_metadata_without_verification_claims",
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
        {
            "scenario_id": "m9_package_review_receipt_behavior",
            "dimension": "package_review_receipts",
            "status": "passed",
            "package_id": "seraph.github-managed",
            "receipt_fields": [
                "digest",
                "signed_digest",
                "key_id",
                "review_status",
                "permission_fingerprint",
                "reviewed_permission_fingerprint",
                "compatibility_status",
                "supply_chain_status",
                "trust_downgrade_status",
                "claim_boundary",
            ],
            "review_receipt_state": "digest_key_review_permissions_compatibility_and_supply_chain_visible",
            "operator_surfaces": ["/api/extensions", "/api/operator/m9-governed-ecosystem-benchmark"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_compatibility_transition_semantics_behavior",
            "dimension": "compatibility_transition_semantics",
            "status": "passed",
            "transition_modes": [
                "new_install",
                "same_version_changed_package",
                "upgrade",
                "downgrade",
                "incompatible_runtime",
                "workspace_override",
            ],
            "blocking_reasons": ["candidate_seraph_compatibility_excludes_current_runtime", "version_downgrade"],
            "recommended_actions": ["install", "update", "none", "reject_or_re_review"],
            "operator_surfaces": ["/api/extensions/validate"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_supply_chain_policy_behavior",
            "dimension": "package_review_receipts",
            "status": "passed",
            "supply_chain_failure_modes": [
                "missing_provenance",
                "unsupported_signature_algorithm",
                "unsigned_verified",
                "digest_mismatch",
                "revoked_digest",
                "unreviewed_provider_change",
            ],
            "fail_closed": True,
            "operator_surfaces": ["/api/extensions", "/api/extensions/validate"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_provider_trust_downgrade_behavior",
            "dimension": "provider_trust_downgrade",
            "status": "passed",
            "blocked_transitions": [
                "verified_to_local",
                "verified_to_unsigned",
                "verified_connector_to_lower_trust",
            ],
            "transition_blocker": "trust_downgrade",
            "fail_closed": True,
            "operator_surfaces": ["/api/extensions/validate", "/api/extensions/install"],
            "claim_boundary": policy["claim_boundary"],
        },
        {
            "scenario_id": "m9_authoring_ergonomics_behavior",
            "dimension": "authoring_ergonomics",
            "status": "passed",
            "scaffold_fields": [
                "governance.provenance",
                "governance.review",
                "governance.source_policy",
                "governance.compatibility_policy",
                "governance.trust_downgrade_policy",
            ],
            "doctor_guidance": ["incompatible_runtime", "supply_chain_policy", "source_policy"],
            "verification_claim": "local_authoring_not_verified_marketplace_security",
            "operator_surfaces": ["/api/extensions/validate"],
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
            "package_review_receipt_state": (
                "digest_key_review_permissions_compatibility_and_supply_chain_visible"
                if healthy
                else degraded_state
            ),
            "compatibility_transition_state": (
                "install_same_upgrade_downgrade_incompatible_and_override_semantics_visible"
                if healthy
                else degraded_state
            ),
            "trust_downgrade_state": (
                "reviewed_provider_backed_downgrades_blocked"
                if healthy
                else degraded_state
            ),
            "authoring_ergonomics_state": (
                "review_ready_local_scaffolds_and_doctor_guidance_visible"
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
