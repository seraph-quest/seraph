"""Batch CA marketplace-grade capability lifecycle receipts.

This module composes existing extension lifecycle, governance, and pack-hardening
foundations into production-oriented marketplace lifecycle receipts. It is
deterministic proof, not production marketplace security, ecosystem superiority,
or third-party package security proof.
"""

from __future__ import annotations

from typing import Any


MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SUITE_NAME = "marketplace_grade_capability_lifecycle"
MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES = (
    "marketplace_lifecycle_install_receipt_behavior",
    "marketplace_lifecycle_update_receipt_behavior",
    "marketplace_lifecycle_downgrade_receipt_behavior",
    "marketplace_lifecycle_disable_receipt_behavior",
    "marketplace_lifecycle_rollback_receipt_behavior",
    "marketplace_lifecycle_review_gate_behavior",
    "marketplace_lifecycle_quarantine_behavior",
    "marketplace_lifecycle_diagnostics_behavior",
    "operator_marketplace_lifecycle_surface_behavior",
)
GOVERNED_CAPABILITY_LIFECYCLE_V2_SUITE_NAME = "governed_capability_lifecycle_v2"
GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES = (
    "capability_lifecycle_permission_delta_behavior",
    "capability_lifecycle_risk_delta_behavior",
    "capability_lifecycle_dependency_graph_behavior",
    "capability_lifecycle_compatibility_resolver_behavior",
    "capability_lifecycle_staged_rollout_behavior",
    "capability_lifecycle_cross_family_coverage_behavior",
    "capability_lifecycle_claim_boundary_behavior",
)
CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SUITE_NAME = "capability_rollback_failure_diagnostics"
CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES = (
    "capability_failed_update_recovery_behavior",
    "capability_rollback_availability_behavior",
    "capability_permission_creep_negative_case_behavior",
    "capability_diagnostics_triage_behavior",
    "capability_quarantine_reentry_review_behavior",
)
MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY = (
    "marketplace_lifecycle_receipts_not_production_secure_marketplace_or_ecosystem_superiority"
)
MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS = (
    "production_secure_marketplace",
    "strongest_extension_ecosystem",
    "third_party_package_security_solved",
    "package_count_superiority",
    "ecosystem_maturity_superiority",
    "full_marketplace_parity",
    "reference_systems_exceeded",
    "full_production_parity",
)


def marketplace_lifecycle_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SUITE_NAME,
            GOVERNED_CAPABILITY_LIFECYCLE_V2_SUITE_NAME,
            CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SUITE_NAME,
        ],
        "claim_boundary": MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
        "lifecycle_policy": (
            "install update downgrade disable rollback review quarantine diagnostics and staged rollout "
            "mutations must carry before after permission risk rollback and failure recovery receipts"
        ),
        "review_policy": (
            "privileged incompatible underdeclared suspicious downgraded or failed-update packages fail closed "
            "or quarantine until operator review"
        ),
        "ecosystem_policy": (
            "package count never substitutes for trust tier compatibility dependency permission risk rollback "
            "diagnostics or guardian policy receipts"
        ),
        "receipt_surfaces": [
            "/api/extensions",
            "/api/extensions/validate",
            "/api/capabilities/overview",
            "/api/operator/marketplace-lifecycle-maturity",
            "/api/operator/m9-governed-ecosystem-benchmark",
            "/api/operator/governed-capability-pack-hardening",
            "/api/operator/benchmark-proof",
        ],
        "blocked_claims": list(MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS),
        "not_claimed": [
            "production_secure_marketplace",
            "third_party_package_security_solved",
            "ecosystem_superiority",
            "package_count_superiority",
            "live_marketplace_attestation",
        ],
    }


def marketplace_lifecycle_receipts() -> list[dict[str, Any]]:
    actions = [
        ("install", "review_required", "create_rollback_snapshot"),
        ("update", "review_required", "restore_previous_version"),
        ("downgrade", "blocked_until_review", "restore_newer_version"),
        ("disable", "allowed_with_degraded_capability_receipt", "reenable_previous_state"),
        ("rollback", "allowed_after_review", "restore_previous_digest"),
        ("review", "operator_required", "record_review_receipt"),
        ("quarantine", "fail_closed", "review_or_remove_package"),
        ("diagnostics", "read_only", "repair_review_or_defer"),
        ("staged_rollout", "canary_only", "roll_back_canary"),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (action, lifecycle_state, recovery_action) in enumerate(actions, start=1):
        receipts.append({
            "receipt_id": f"ca-lifecycle-{index:02d}-{action}",
            "action": action,
            "lifecycle_state": lifecycle_state,
            "before": {
                "version": "1.4.0" if action in {"update", "downgrade", "rollback"} else None,
                "enabled": action not in {"install", "quarantine"},
                "trust_tier": "reviewed",
                "permission_envelope": ["files.read", "workflow.run"],
            },
            "after": {
                "version": "1.5.0" if action == "update" else "1.3.0" if action == "downgrade" else "1.4.0",
                "enabled": action not in {"disable", "quarantine"},
                "trust_tier": "review_required" if action in {"install", "update", "downgrade"} else "reviewed",
                "permission_envelope": (
                    ["files.read", "workflow.run", "network.request"]
                    if action in {"install", "update"}
                    else ["files.read", "workflow.run"]
                ),
            },
            "permission_delta": {
                "added": ["network.request"] if action in {"install", "update"} else [],
                "removed": ["network.request"] if action in {"rollback", "disable"} else [],
                "requires_review": action in {"install", "update", "downgrade"},
            },
            "risk_delta": {
                "risk_before": "medium",
                "risk_after": "high" if action in {"install", "update", "downgrade"} else "medium",
                "reason": "permission_or_version_boundary_changed" if action in {"install", "update", "downgrade"} else "no_privilege_expansion",
            },
            "rollback": {
                "available": action != "diagnostics",
                "action": recovery_action,
                "receipt_required": action in {"install", "update", "downgrade", "rollback", "quarantine"},
            },
            "failure_recovery": {
                "fails_closed": action in {"downgrade", "quarantine", "staged_rollout"},
                "recovery_action": recovery_action,
                "operator_visible": True,
            },
            "operator_receipt_id": f"operator:marketplace-ca:{action}",
        })
    return receipts


def capability_family_coverage_receipts() -> list[dict[str, Any]]:
    families = [
        ("skills", "declarative_prompt_surface"),
        ("workflows", "workflow_runtime_surface"),
        ("runbooks", "operator_macro_surface"),
        ("starter_packs", "bootstrap_composition_surface"),
        ("connectors", "source_or_action_surface"),
        ("browser_providers", "computer_use_surface"),
        ("messaging_connectors", "reach_surface"),
        ("node_adapters", "device_or_companion_surface"),
        ("memory_providers", "guardian_memory_surface"),
        ("voice_media_profiles", "voice_media_surface"),
        ("managed_connectors", "authenticated_connector_surface"),
    ]
    return [
        {
            "family": family,
            "surface": surface,
            "publisher_visible": True,
            "trust_tier_visible": True,
            "compatibility_visible": True,
            "dependency_graph_visible": True,
            "permission_delta_visible": True,
            "risk_delta_visible": True,
            "rollback_visible": True,
            "diagnostics_visible": True,
        }
        for family, surface in families
    ]


def lifecycle_negative_case_receipts() -> list[dict[str, Any]]:
    return [
        {
            "case_id": "incompatible-version",
            "state": "blocked",
            "reason": "runtime_version_outside_manifest_range",
            "fails_closed": True,
            "recovery": "choose_compatible_version_or_defer",
        },
        {
            "case_id": "underdeclared-permissions",
            "state": "blocked",
            "reason": "runtime_permission_need_exceeds_manifest",
            "fails_closed": True,
            "recovery": "fresh_review_with_expanded_permission_envelope",
        },
        {
            "case_id": "suspicious-digest",
            "state": "quarantined",
            "reason": "candidate_digest_or_signature_mismatch",
            "fails_closed": True,
            "recovery": "remove_or_reverify_publisher_key",
        },
        {
            "case_id": "failed-update",
            "state": "rolled_back",
            "reason": "update_validation_failed_after_staging",
            "fails_closed": True,
            "recovery": "restore_previous_digest_and_runtime_state",
        },
        {
            "case_id": "permission-creep",
            "state": "quarantined",
            "reason": "permission_delta_after_review",
            "fails_closed": True,
            "recovery": "operator_review_or_revoke_runtime_contributions",
        },
    ]


def staged_rollout_receipts() -> list[dict[str, Any]]:
    return [
        {
            "rollout_id": "ca-canary-skill-pack",
            "package_family": "skills",
            "stage": "canary",
            "promotion_requires": ["review_receipt", "green_benchmark", "rollback_ready"],
            "rollback_ready": True,
        },
        {
            "rollout_id": "ca-managed-connector-update",
            "package_family": "managed_connectors",
            "stage": "review_hold",
            "promotion_requires": ["connector_health_green", "credential_boundary_review"],
            "rollback_ready": True,
        },
    ]


def build_marketplace_lifecycle_contract() -> dict[str, Any]:
    lifecycle = marketplace_lifecycle_receipts()
    families = capability_family_coverage_receipts()
    negative_cases = lifecycle_negative_case_receipts()
    rollouts = staged_rollout_receipts()
    policy = marketplace_lifecycle_policy_payload()
    return {
        "summary": {
            "operator_status": "marketplace_lifecycle_maturity_receipts_visible",
            "lifecycle_action_count": len(lifecycle),
            "family_count": len(families),
            "negative_case_count": len(negative_cases),
            "staged_rollout_count": len(rollouts),
            "permission_delta_receipt_count": sum(
                1 for item in lifecycle
                if item["permission_delta"]["added"] or item["permission_delta"]["removed"]
            ),
            "risk_delta_receipt_count": sum(1 for item in lifecycle if item.get("risk_delta")),
            "rollback_receipt_count": sum(1 for item in lifecycle if item["rollback"]["available"]),
            "quarantine_receipt_count": sum(
                1 for item in negative_cases
                if item["state"] == "quarantined"
            ),
            "failed_update_recovery_visible": any(item["case_id"] == "failed-update" for item in negative_cases),
            "cross_family_coverage_visible": len(families) >= 10,
            "package_count_substitution_blocked": True,
            "claim_boundary": MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
        },
        "lifecycle_receipts": lifecycle,
        "family_coverage": families,
        "negative_cases": negative_cases,
        "staged_rollouts": rollouts,
        "policy": policy,
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Marketplace lifecycle scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_marketplace_lifecycle_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SUITE_NAME,
        GOVERNED_CAPABILITY_LIFECYCLE_V2_SUITE_NAME,
        CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SUITE_NAME,
    ])


async def build_marketplace_lifecycle_report() -> dict[str, Any]:
    summary = await _run_marketplace_lifecycle_suites()
    contract = build_marketplace_lifecycle_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "marketplace_lifecycle_maturity_ci_gated_operator_visible"
                if healthy
                else "marketplace_lifecycle_maturity_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES)
                + len(GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES)
                + len(CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SUITE_NAME: list(
                MARKETPLACE_GRADE_CAPABILITY_LIFECYCLE_SCENARIO_NAMES
            ),
            GOVERNED_CAPABILITY_LIFECYCLE_V2_SUITE_NAME: list(
                GOVERNED_CAPABILITY_LIFECYCLE_V2_SCENARIO_NAMES
            ),
            CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SUITE_NAME: list(
                CAPABILITY_ROLLBACK_FAILURE_DIAGNOSTICS_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="marketplace_lifecycle_maturity"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
