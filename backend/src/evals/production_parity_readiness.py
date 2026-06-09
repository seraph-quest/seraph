from __future__ import annotations

from typing import Any


PRODUCTION_PARITY_READINESS_SUITE_NAME = "production_parity_readiness"
PRODUCTION_PARITY_READINESS_SCENARIO_NAMES = (
    "production_parity_batch_contract_behavior",
    "production_parity_claim_gate_behavior",
    "production_parity_proof_gate_behavior",
    "production_parity_project_board_contract_behavior",
    "production_parity_duplicate_scope_boundary_behavior",
    "production_parity_validation_receipt_behavior",
    "operator_production_parity_readiness_surface_behavior",
)

PRODUCTION_PARITY_READINESS_CLAIM_BOUNDARY = (
    "readiness_contract_for_production_parity_train_not_full_parity_or_superiority"
)

PRODUCTION_PARITY_BLOCKED_CLAIMS = (
    "fully_at_parity",
    "reference_systems_exceeded",
    "production_ready",
    "secure_private_by_default",
    "ironclaw_class_secure_execution",
    "broad_openclaw_class_reach",
    "voice_or_multimodal_parity",
    "solved_durable_workflows",
    "production_secure_marketplace",
)

REQUIRED_PROJECT_FIELDS = (
    "Queue",
    "Lane",
    "Priority",
    "Size",
    "Status",
    "Code Review",
    "PR",
)


def production_parity_batch_contracts() -> list[dict[str, Any]]:
    return [
        {
            "issue": 476,
            "batch": "BV",
            "title": "production parity readiness, claim gates, and integration proof harness",
            "lane": "Docs / Meta",
            "proof_suites": [PRODUCTION_PARITY_READINESS_SUITE_NAME],
            "operator_receipt_target": "/api/operator/production-parity-readiness",
            "validation_classes": [
                "claim_ledger_check",
                "benchmark_catalog_api_tests",
                "project_receipt_readback",
                "critic_contrarian_review",
            ],
            "negative_cases": [
                "readiness_report_missing_batch",
                "forbidden_claim_in_docs_or_pr",
                "project_field_receipt_missing",
                "duplicate_foundation_scope_reopened",
            ],
            "review_roles": [
                "Planner",
                "Security/Trust",
                "Docs/Board",
                "Integrator",
                "Critic/Contrarian",
            ],
            "blocked_claims": list(PRODUCTION_PARITY_BLOCKED_CLAIMS),
        },
        {
            "issue": 477,
            "batch": "BW",
            "title": "secure-host architectural isolation and privileged-path hardening",
            "lane": "Trust Boundaries",
            "proof_suites": [
                "production_secure_host_hardening",
                "secure_capability_host_live_isolation_v2",
            ],
            "operator_receipt_target": "/api/operator/secure-capability-host-hardening",
            "validation_classes": [
                "privileged_path_negative_cases",
                "browser_session_partitioning",
                "secret_egress_redaction",
                "trust_boundary_regression_matrix",
            ],
            "negative_cases": [
                "secret_ref_egress",
                "browser_cookie_profile_bleed",
                "workspace_escape",
                "provider_trust_expansion",
                "revoked_extension_runtime_access",
            ],
            "review_roles": ["Security/Trust", "Worker", "Integrator", "Critic/Contrarian"],
            "blocked_claims": [
                "secure_private_by_default",
                "ironclaw_class_secure_execution",
                "production_ready",
            ],
        },
        {
            "issue": 478,
            "batch": "BX",
            "title": "production durable orchestration and multi-agent workflow control",
            "lane": "Runtime Reliability",
            "proof_suites": [
                "production_durable_orchestration",
                "durable_workflow_engine_v2",
            ],
            "operator_receipt_target": "/api/operator/durable-workflow-engine",
            "validation_classes": [
                "crash_resume_replay",
                "multi_agent_recovery",
                "heartbeat_reactive_trigger_receipts",
                "cockpit_repair_controls",
            ],
            "negative_cases": [
                "crash_resume_drops_state",
                "replay_or_resume_boundary_drift",
                "delegated_agent_ownership_loss",
                "repair_control_without_operator_receipt",
            ],
            "review_roles": ["Worker", "Security/Trust", "Integrator", "Critic/Contrarian"],
            "blocked_claims": [
                "solved_durable_workflows",
                "production_ready",
            ],
        },
        {
            "issue": 479,
            "batch": "BY",
            "title": "broad live reach, browser reliability, and voice/media runtime hardening",
            "lane": "Presence and Reach",
            "proof_suites": [
                "production_reach_channel_hardening",
                "browser_computer_use_reliability_v2",
                "guardian_safe_voice_media_runtime",
            ],
            "operator_receipt_target": "/api/operator/benchmark-proof",
            "validation_classes": [
                "live_channel_e2e_replay",
                "browser_recovery_replay",
                "voice_media_privacy_receipts",
                "reach_revocation_fail_closed",
            ],
            "negative_cases": [
                "channel_revocation_ignored",
                "browser_session_recovery_loses_partition",
                "voice_transcript_or_media_leak",
                "approval_handoff_bypassed",
            ],
            "review_roles": ["Security/Trust", "Worker", "Docs/Board", "Critic/Contrarian"],
            "blocked_claims": [
                "broad_openclaw_class_reach",
                "voice_or_multimodal_parity",
                "production_ready",
            ],
        },
        {
            "issue": 480,
            "batch": "BZ",
            "title": "live guardian learning, intervention quality, and memory-provider outcome proof",
            "lane": "Guardian Intelligence",
            "proof_suites": [
                "live_guardian_learning_quality",
                "guardian_intervention_outcome_cohorts",
                "memory_provider_ecosystem_maturity_v1",
            ],
            "operator_receipt_target": "/api/operator/guardian-learning-arbitration",
            "validation_classes": [
                "long_horizon_outcome_replay",
                "negative_intervention_cohorts",
                "memory_provider_quality_receipts",
                "operator_correction_feedback",
            ],
            "negative_cases": [
                "false_positive_intervention_growth",
                "false_negative_followthrough_gap",
                "stale_provider_evidence_overrides_canonical_memory",
                "operator_correction_not_reflected_in_policy",
            ],
            "review_roles": ["Memory/Learning", "Worker", "Security/Trust", "Critic/Contrarian"],
            "existing_proof_floors": [
                "guardian_memory_quality",
                "m6_memory_superiority",
                "memory_provider_quality_gate",
                "guardian_user_model_restraint",
                "guardian_learning_arbitration_v2",
                "live_long_horizon_eval_replay_v1",
            ],
            "blocked_claims": [
                "reference_systems_exceeded",
                "guardian_intelligence_superiority",
                "production_ready",
            ],
        },
        {
            "issue": 481,
            "batch": "CA",
            "title": "marketplace-grade capability lifecycle, review, rollback, and ecosystem maturity",
            "lane": "Ecosystem and Leverage",
            "proof_suites": [
                "marketplace_grade_capability_lifecycle",
                "governed_capability_hardening_v2",
            ],
            "operator_receipt_target": "/api/operator/governed-capability-pack-hardening",
            "validation_classes": [
                "install_update_downgrade_rollback",
                "supply_chain_suspicion_fail_closed",
                "review_queue_diagnostics",
                "connector_health_repair",
            ],
            "negative_cases": [
                "permission_creep",
                "supply_chain_suspicion_ignored",
                "rollback_missing_after_failed_update",
                "connector_degradation_overclaimed",
            ],
            "review_roles": ["Security/Trust", "Worker", "Docs/Board", "Critic/Contrarian"],
            "blocked_claims": [
                "production_secure_marketplace",
                "reference_systems_exceeded",
                "production_ready",
            ],
        },
        {
            "issue": 482,
            "batch": "CB",
            "title": "production operator cockpit control and end-to-end parity verification",
            "lane": "Embodied UX",
            "proof_suites": [
                "production_operator_control_parity",
                "production_parity_train",
            ],
            "operator_receipt_target": "/api/operator/benchmark-proof",
            "validation_classes": [
                "end_to_end_parity_train_replay",
                "operator_debug_recovery_controls",
                "claim_ledger_completion_audit",
                "final_critic_contrarian_review",
            ],
            "negative_cases": [
                "parity_train_claim_without_all_batch_merges",
                "operator_recovery_control_missing_receipt",
                "stale_project_board_field",
                "unsupported_competitor_superiority_claim",
            ],
            "review_roles": [
                "Planner",
                "Security/Trust",
                "Memory/Learning",
                "Docs/Board",
                "Integrator",
                "Critic/Contrarian",
            ],
            "blocked_claims": list(PRODUCTION_PARITY_BLOCKED_CLAIMS),
        },
    ]


def production_parity_duplicate_guardrails() -> list[dict[str, str]]:
    return [
        {
            "anchor": "#424",
            "reason": "M1 capability manifest foundation remains closed; BV only defines production train readiness.",
        },
        {
            "anchor": "#436",
            "reason": "World-class strategy hub remains historical; BV does not reopen M0-M9 strategy scope.",
        },
        {
            "anchor": "#468",
            "reason": "The proof train completed deterministic floors; BV gates the later production-grade train.",
        },
        {
            "anchor": "PR #473",
            "reason": "Merged proof-train receipts are evidence inputs, not duplicate live execution work.",
        },
        {
            "anchor": "#475",
            "reason": "The production parity parent remains the train hub; BV must not replace or close the whole train.",
        },
        {
            "anchor": "#477-#482",
            "reason": "Later production batches own implementation proof; BV defines their gate contract only.",
        },
        {
            "anchor": "#438/#439/#440/#441/#467/#470/#471/#472",
            "reason": "Closed proof anchors are prerequisite floors and duplicate-scope guards, not reopened implementation issues.",
        },
    ]


def production_parity_receipt_schema() -> list[str]:
    return [
        "batch_id",
        "owner_issue",
        "proof_suite",
        "receipt_surface",
        "authority_source",
        "actor_or_session",
        "trust_boundary",
        "credential_or_evidence_exposure",
        "policy_decision",
        "redaction_status",
        "blocked_claims",
        "residual_risk",
        "recovery_action",
        "linked_proof_run",
    ]


def production_parity_validation_plan() -> dict[str, Any]:
    return {
        "required_project_fields": list(REQUIRED_PROJECT_FIELDS),
        "status_defaults": {
            "new_batch": {
                "Status": "Todo",
                "Code Review": "Not Ready",
                "PR": "Not Ready",
            },
            "active_batch": {
                "Queue": "Now",
                "Status": "In Progress",
            },
            "open_pr": {
                "PR": "Open",
                "Code Review": "Pending",
            },
            "merged_pr": {
                "PR": "Merged",
                "Status": "Done",
            },
        },
        "pr_receipts": [
            "linked_parent_or_batch_issue",
            "team_passes_and_capacity_limitations",
            "critic_contrarian_disposition",
            "focused_validation",
            "claim_boundary_review",
            "board_field_receipt",
            "current_source_status_for_competitor_claims",
        ],
        "validation_commands": [
            "git diff --check",
            "python scripts/check_strategy_claims.py",
            "uv run pytest backend/tests/test_eval_harness.py backend/tests/test_operator_api.py backend/tests/test_strategy_claims.py",
        ],
        "source_refresh_policy": (
            "Any new Hermes, OpenClaw, IronClaw, model, API, security, reach, or competitor parity claim must cite "
            "current official source URLs with access dates or remain marked Unknown/unverified."
        ),
        "receipt_schema": production_parity_receipt_schema(),
    }


def production_parity_readiness_policy_payload() -> dict[str, Any]:
    return {
        "claim_boundary": PRODUCTION_PARITY_READINESS_CLAIM_BOUNDARY,
        "blocked_claims": list(PRODUCTION_PARITY_BLOCKED_CLAIMS),
        "operator_visibility": "readiness_contract_and_batch_proof_targets_visible",
        "receipt_surfaces": [
            "/api/operator/production-parity-readiness",
            "/api/operator/benchmark-proof",
            "GitHub issue #475",
            "GitHub issues #476-#482",
            "GitHub Project fields",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "current_source_requirement": (
            "No new competitor-dependent parity, security, reach, voice, marketplace, or superiority claim may be "
            "introduced without current official source URLs and dates."
        ),
        "ci_gate_mode": "required_benchmark_suite",
        "not_claimed": [
            "full_parity_achieved",
            "reference_systems_exceeded",
            "production_ready_agent",
            "production_secure_execution",
        ],
    }


def production_parity_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "missing_batch_proof_path",
            "severity": "high",
            "summary": "A production parity batch lacks a named proof suite, operator receipt target, or validation class.",
        },
        {
            "name": "claim_boundary_drift",
            "severity": "high",
            "summary": "A doc, issue, or PR uses full parity, superiority, production-ready, secure/private, broad reach, voice parity, or marketplace wording before the ledger permits it.",
        },
        {
            "name": "duplicate_foundation_scope",
            "severity": "medium",
            "summary": "A production batch reopens a closed M0-M9/proof-train anchor instead of targeting the next production-grade gap.",
        },
        {
            "name": "board_receipt_gap",
            "severity": "medium",
            "summary": "A tracked issue lacks Queue, Lane, Priority, Size, Status, PR, or Code Review project-field evidence.",
        },
        {
            "name": "weak_operator_receipt",
            "severity": "medium",
            "summary": "The batch can pass tests but does not expose the proof or remaining claim boundary to operators.",
        },
    ]


def production_parity_readiness_summary(healthy: bool, active_failure_count: int = 0) -> dict[str, Any]:
    batches = production_parity_batch_contracts()
    return {
        "suite_name": PRODUCTION_PARITY_READINESS_SUITE_NAME,
        "benchmark_posture": (
            "production_parity_readiness_ci_gated_operator_visible"
            if healthy
            else "production_parity_readiness_regressions_detected_operator_visible"
        ),
        "operator_status": "production_parity_readiness_visible",
        "scenario_count": len(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES),
        "batch_count": len(batches),
        "future_batch_count": len([batch for batch in batches if batch["issue"] != 476]),
        "blocked_claim_count": len(PRODUCTION_PARITY_BLOCKED_CLAIMS),
        "proof_path_count": sum(len(batch["proof_suites"]) for batch in batches),
        "negative_case_count": sum(len(batch["negative_cases"]) for batch in batches),
        "review_role_count": len({role for batch in batches for role in batch["review_roles"]}),
        "operator_receipt_target_count": len({batch["operator_receipt_target"] for batch in batches}),
        "project_field_count": len(REQUIRED_PROJECT_FIELDS),
        "receipt_schema_field_count": len(production_parity_receipt_schema()),
        "duplicate_guardrail_count": len(production_parity_duplicate_guardrails()),
        "failure_mode_count": len(production_parity_failure_taxonomy()),
        "active_failure_count": active_failure_count,
        "claim_boundary": PRODUCTION_PARITY_READINESS_CLAIM_BOUNDARY,
        "completion_state": "readiness_contract_only_full_parity_unproven",
    }


def _production_parity_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Production parity readiness scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_production_parity_readiness_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([PRODUCTION_PARITY_READINESS_SUITE_NAME])


async def build_production_parity_readiness_report() -> dict[str, Any]:
    summary = await _run_production_parity_readiness_suite()
    failure_report = _production_parity_failure_report(summary)
    healthy = summary.failed == 0
    return {
        "summary": production_parity_readiness_summary(
            healthy=healthy,
            active_failure_count=summary.failed,
        ),
        "scenario_names": list(PRODUCTION_PARITY_READINESS_SCENARIO_NAMES),
        "batch_contracts": production_parity_batch_contracts(),
        "duplicate_guardrails": production_parity_duplicate_guardrails(),
        "validation_plan": production_parity_validation_plan(),
        "failure_taxonomy": production_parity_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": production_parity_readiness_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
