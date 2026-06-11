"""Batch CQ final claim-lift and critic-audit receipts.

This module reconciles current-source competitor evidence, production-train
batch state, claim-ledger boundaries, and operator-visible proof surfaces. It
is a final claim-lift audit gate, not blanket full-parity, superiority, or
production-ready evidence.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
from typing import Any


FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME = "final_source_backed_parity_audit"
FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES = (
    "final_current_source_coverage_behavior",
    "final_competitor_pressure_mapping_behavior",
    "final_batch_completion_evidence_behavior",
    "final_residual_gap_boundary_behavior",
    "final_source_date_freshness_behavior",
    "final_source_access_caveat_behavior",
)
FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME = "final_claim_ledger_reconciliation"
FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES = (
    "final_forbidden_claim_block_behavior",
    "final_allowed_wording_scope_behavior",
    "final_claim_ledger_issue_link_behavior",
    "final_claim_boundary_operator_surface_behavior",
    "final_claim_lift_matrix_behavior",
    "final_critic_disposition_required_behavior",
)
OPERATOR_FINAL_PARITY_READINESS_REPORT_SUITE_NAME = "operator_final_parity_readiness_report"
OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES = (
    "operator_final_readiness_report_surface_behavior",
    "operator_final_board_reconciliation_behavior",
    "operator_final_benchmark_aggregate_behavior",
    "operator_final_residual_risk_visibility_behavior",
    "operator_final_cq_merged_state_behavior",
    "operator_final_no_false_completion_behavior",
)
POST_CQ_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME = "post_cq_claim_ledger_reconciliation"
POST_CQ_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES = (
    "post_cq_issue_pr_project_reconciliation_behavior",
    "post_cq_claim_ledger_allowed_wording_behavior",
    "post_cq_blocked_claim_boundary_behavior",
    "post_cq_critic_disposition_behavior",
    "post_cq_no_duplicate_tracking_behavior",
)
REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SUITE_NAME = "reference_system_source_refresh_v2"
REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SCENARIO_NAMES = (
    "reference_system_source_urls_access_date_behavior",
    "reference_system_pressure_axis_mapping_behavior",
    "reference_system_access_caveat_behavior",
    "reference_system_stale_source_uncertainty_behavior",
    "reference_system_claim_lift_block_behavior",
)
FALSE_COMPLETION_SCAN_V2_SUITE_NAME = "false_completion_scan_v2"
FALSE_COMPLETION_SCAN_V2_SCENARIO_NAMES = (
    "false_completion_docs_scan_behavior",
    "false_completion_code_operator_scan_behavior",
    "false_completion_issue_pr_scan_behavior",
    "false_completion_claim_ledger_replacement_behavior",
    "false_completion_final_gate_behavior",
)
PRODUCTION_READINESS_SOAK_V1_SUITE_NAME = "production_readiness_soak_v1"
PRODUCTION_READINESS_SOAK_V1_SCENARIO_NAMES = (
    "production_readiness_runtime_security_soak_behavior",
    "production_readiness_reach_learning_operator_soak_behavior",
    "production_readiness_marketplace_browser_soak_behavior",
    "production_readiness_residual_risk_behavior",
    "production_readiness_claim_boundary_behavior",
)
FINAL_FULL_PARITY_CLAIM_LIFT_V1_SUITE_NAME = "final_full_parity_claim_lift_v1"
FINAL_FULL_PARITY_CLAIM_LIFT_V1_SCENARIO_NAMES = (
    "final_full_parity_da_dg_reconciliation_behavior",
    "final_full_parity_claim_ledger_scl_043_050_behavior",
    "final_full_parity_broad_claim_block_behavior",
    "final_full_parity_allowed_wording_behavior",
    "final_full_parity_critic_disposition_behavior",
)
REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SUITE_NAME = "reference_system_source_refresh_v3"
REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SCENARIO_NAMES = (
    "reference_system_source_refresh_v3_urls_dates_behavior",
    "reference_system_source_refresh_v3_pressure_mapping_behavior",
    "reference_system_source_refresh_v3_access_caveat_behavior",
    "reference_system_source_refresh_v3_claim_lift_block_behavior",
    "reference_system_source_refresh_v3_stale_source_guard_behavior",
)
FALSE_COMPLETION_SCAN_V3_SUITE_NAME = "false_completion_scan_v3"
FALSE_COMPLETION_SCAN_V3_SCENARIO_NAMES = (
    "false_completion_v3_docs_scan_behavior",
    "false_completion_v3_code_operator_scan_behavior",
    "false_completion_v3_github_tracking_scan_behavior",
    "false_completion_v3_stale_pr_closure_behavior",
    "false_completion_v3_final_gate_behavior",
)
BOARD_PR_ISSUE_RECONCILIATION_V3_SUITE_NAME = "board_pr_issue_reconciliation_v3"
BOARD_PR_ISSUE_RECONCILIATION_V3_SCENARIO_NAMES = (
    "board_pr_issue_da_dg_done_merged_passed_behavior",
    "board_pr_issue_dh_active_branch_behavior",
    "board_pr_issue_parent_program_state_behavior",
    "board_pr_issue_stale_pr_closed_behavior",
    "board_pr_issue_project_field_receipt_behavior",
)
FINAL_PARITY_AUDIT_CLAIM_BOUNDARY = (
    "final_claim_lift_audit_permits_only_exact_bounded_wording_not_full_parity"
)
FINAL_PARITY_AUDIT_BLOCKED_CLAIMS = (
    "fully_at_parity",
    "reference_systems_exceeded",
    "production_ready_product",
    "secure_private_by_default",
    "ironclaw_class_secure_execution",
    "openclaw_class_reach",
    "voice_or_multimodal_parity",
    "safe_browser_automation",
    "full_browser_parity",
    "production_secure_marketplace",
    "best_cockpit",
    "world_class_cockpit",
    "guardian_intelligence_superiority",
    "memory_superiority",
    "exactly_once_or_crash_proof_orchestration",
)
POST_CQ_CLAIM_READINESS_CLAIM_BOUNDARY = (
    "post_cq_final_audit_permits_only_exact_bounded_receipt_wording_not_full_parity"
)
POST_CQ_CLAIM_READINESS_ALLOWED_WORDING = (
    "Seraph has completed a post-CQ production-evidence claim-readiness audit "
    "with bounded receipts."
)
POST_CQ_CLAIM_READINESS_BLOCKED_CLAIMS = (
    "fully_at_parity",
    "reference_systems_exceeded",
    "production_ready_product",
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "openclaw_class_reach",
    "voice_or_multimodal_parity",
    "safe_browser_automation",
    "safe_autonomous_browser_computer_use",
    "full_browser_parity",
    "production_secure_marketplace",
    "third_party_package_security_solved",
    "solved_operator_control",
    "best_cockpit",
    "world_class_cockpit",
    "guardian_intelligence_superiority",
    "solved_live_or_long_term_learning",
    "live_human_outcome_superiority",
    "memory_superiority",
    "full_memory_provider_parity",
    "exactly_once_or_crash_proof_orchestration",
    "hardware_backed_tee_cvm_wasm_or_container_isolation_implemented_or_certified",
)
POST_CQ_FALSE_COMPLETION_FORBIDDEN_PHRASES = (
    "Seraph is fully at parity",
    "Seraph has exceeded Hermes/OpenClaw/IronClaw",
    "Seraph is production-ready",
    "Seraph is secure/private by default",
    "Seraph has IronClaw-class secure execution",
    "Seraph has OpenClaw-class reach",
    "Seraph has safe browser automation",
    "Seraph has safe autonomous browser/computer use",
    "Seraph has full browser parity",
    "Seraph has a production-secure marketplace",
    "Third-party package security is solved",
    "Operator control is solved",
    "Seraph has guardian intelligence superiority",
    "Seraph has memory superiority",
    "Seraph has exactly-once or crash-proof orchestration",
    "TEE/CVM/Wasm/container isolation is implemented or certified",
)
POST_CQ_FALSE_COMPLETION_ALLOWED_CONTEXT_MARKERS = (
    "blocked",
    "blocked_claims",
    "not claim",
    "not claimed",
    "not_claimed",
    "not itself prove",
    "without claiming",
    "does not permit",
    "does not prove",
    "remain blocked",
    "remains blocked",
    "still blocked",
    "aspirational",
    "forbidden",
    "replacement",
    "false-completion",
    "false_completion",
)
FINAL_PRODUCTION_PARITY_CLAIM_BOUNDARY = (
    "final_production_parity_gate_exposes_da_dg_soak_readiness_and_claim_reconciliation_without_full_parity_claim_lift"
)
FINAL_PRODUCTION_PARITY_ALLOWED_WORDING = (
    "Seraph has completed a final production-readiness reconciliation and claim-lift evidence gate "
    "with bounded DA-DG receipts."
)
FINAL_PRODUCTION_PARITY_BLOCKED_CLAIMS = (
    "fully_at_parity",
    "reference_systems_exceeded",
    "production_ready_product",
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "openclaw_class_reach",
    "complete_channel_coverage",
    "voice_or_multimodal_parity",
    "always_available_operation",
    "safe_browser_automation",
    "safe_autonomous_browser_computer_use",
    "full_browser_parity",
    "production_secure_marketplace",
    "third_party_package_security_solved",
    "solved_operator_control",
    "best_cockpit",
    "world_class_cockpit",
    "guardian_intelligence_superiority",
    "solved_live_or_long_term_learning",
    "live_human_outcome_superiority",
    "memory_superiority",
    "full_memory_provider_parity",
    "exactly_once_or_crash_proof_orchestration",
    "hardware_backed_tee_cvm_wasm_or_container_isolation_implemented_or_certified",
)


def final_parity_audit_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME,
            FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
            OPERATOR_FINAL_PARITY_READINESS_REPORT_SUITE_NAME,
        ],
        "claim_boundary": FINAL_PARITY_AUDIT_CLAIM_BOUNDARY,
        "source_policy": (
            "final competitor-dependent claims require current official/source-backed URLs, access dates, "
            "pressure-axis mapping, and stale-source blocking before wording can strengthen"
        ),
        "board_policy": (
            "parent and batch issues remain the execution layer; final readiness must reconcile issue state, "
            "Project fields, PR state, proof suites, docs, and operator surfaces"
        ),
        "claim_policy": (
            "full parity, superiority, production-ready, secure/private, IronClaw-class, OpenClaw-class, "
            "best-cockpit, and solved-browser wording remains blocked unless exact claim-ledger wording allows it"
        ),
        "receipt_surfaces": [
            "/api/operator/final-parity-readiness-report",
            "/api/operator/benchmark-proof",
            "/api/operator/production-operator-control-parity",
            "/api/operator/dense-operator-recovery-control",
            "/api/operator/production-sla-orchestration",
            "/api/operator/production-reach-voice-mobile",
            "/api/operator/independent-learning-memory-parity",
            "/api/operator/browser-provider-usability-proof",
            "/api/operator/safe-autonomous-browser-computer-use",
            "/api/operator/live-marketplace-attestation-proof",
            "/api/operator/production-marketplace-security",
            "docs/research/19-strategy-claim-ledger.md",
            "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
            "docs/implementation/16-agent-parity-execution-roadmap.md",
        ],
        "blocked_claims": list(FINAL_PARITY_AUDIT_BLOCKED_CLAIMS),
        "not_claimed": [
            "full_product_parity",
            "reference_systems_exceeded",
            "production_ready",
            "ironclaw_class_secure_execution",
            "openclaw_class_reach",
            "safe_autonomous_browser_computer_use",
        ],
    }


def current_competitor_source_receipts() -> list[dict[str, Any]]:
    receipts = [
        {
            "system": "Hermes",
            "source_id": "hermes-features-overview",
            "url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/overview/",
            "checked_on": "2026-06-10",
            "source_kind": "official_docs",
            "pressure_axes": [
                "tool_runtime_breadth",
                "scheduled_tasks",
                "subagent_delegation",
                "browser_automation",
                "voice_media",
                "provider_routing",
                "memory_providers",
                "api_server",
                "plugins",
            ],
            "claim_use": "source_backed_pressure_only",
            "residual_gap": "Seraph proof receipts are broad but do not prove Hermes-wide raw runtime breadth or voice/browser parity.",
        },
        {
            "system": "Hermes",
            "source_id": "hermes-tools-toolsets",
            "url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/",
            "checked_on": "2026-06-10",
            "source_kind": "official_docs",
            "pressure_axes": [
                "web_search",
                "terminal_files",
                "browser_text_vision",
                "media_generation",
                "agent_orchestration",
                "memory_session_search",
                "automation_delivery",
                "container_security",
            ],
            "claim_use": "source_backed_pressure_only",
            "residual_gap": "Seraph must keep tool/runtime claims scoped to named suites and operator receipts.",
        },
        {
            "system": "OpenClaw",
            "source_id": "openclaw-control-ui",
            "url": "https://docs.openclaw.ai/web/control-ui",
            "checked_on": "2026-06-10",
            "source_kind": "official_docs",
            "pressure_axes": [
                "web_control_ui",
                "gateway_websocket",
                "device_pairing",
                "operator_identity",
                "runtime_config",
                "activity_tab",
                "pwa_web_push",
            ],
            "claim_use": "source_backed_pressure_only",
            "residual_gap": "Seraph has cockpit receipts but cannot claim OpenClaw-class reach or control-plane breadth.",
        },
        {
            "system": "OpenClaw",
            "source_id": "openclaw-browser",
            "url": "https://docs.openclaw.ai/tools/browser",
            "checked_on": "2026-06-10",
            "source_kind": "official_docs",
            "pressure_axes": [
                "separate_browser_profile",
                "deterministic_tab_control",
                "agent_actions",
                "snapshots_screenshots_pdfs",
                "stale_ref_recovery",
                "multi_profile_support",
            ],
            "claim_use": "source_backed_pressure_only",
            "residual_gap": (
                "Batch CP adds bounded browser/computer-use safety receipts, while blanket safe "
                "browser automation and full browser parity wording remain final-audit gated."
            ),
        },
        {
            "system": "OpenClaw",
            "source_id": "openclaw-plugins",
            "url": "https://docs.openclaw.ai/tools/plugin",
            "checked_on": "2026-06-10",
            "source_kind": "official_docs",
            "pressure_axes": [
                "clawhub_discovery",
                "npm_git_local_install",
                "compatibility_resolution",
                "gateway_restart_reload",
                "runtime_inspect",
                "plugin_allow_deny",
            ],
            "claim_use": "source_backed_pressure_only",
            "residual_gap": "Seraph marketplace receipts remain bounded and do not prove production-secure marketplace superiority.",
        },
        {
            "system": "IronClaw",
            "source_id": "ironclaw-security-site",
            "url": "https://www.ironclaw.com/",
            "checked_on": "2026-06-10",
            "source_kind": "official_site",
            "pressure_axes": [
                "encrypted_vault",
                "endpoint_allowlist",
                "per_tool_wasm_sandbox",
                "rust_memory_safety",
                "tee_deployment",
                "credential_injection_boundary",
            ],
            "claim_use": "source_backed_pressure_only",
            "residual_gap": "Seraph has bounded isolation/security receipts but not IronClaw-class TEE/Wasm/vault proof.",
        },
        {
            "system": "IronClaw",
            "source_id": "ironclaw-feature-parity-matrix",
            "url": "https://raw.githubusercontent.com/nearai/ironclaw/staging/FEATURE_PARITY.md",
            "checked_on": "2026-06-10",
            "source_kind": "source_repository_matrix",
            "source_last_reviewed": "2026-03-10",
            "pressure_axes": [
                "control_plane",
                "gateway_endpoints",
                "web_dashboard",
                "channels",
                "session_management",
                "http_api",
                "diagnostics",
                "plugins_extensions",
                "memory",
            ],
            "claim_use": "source_backed_pressure_only",
            "residual_gap": "The matrix supports treating IronClaw as runtime pressure, not only as a security wrapper.",
        },
    ]
    for receipt in receipts:
        receipt["accessed_on"] = "2026-06-10"
        receipt["access_status"] = "reachable"
        receipt["verification_method"] = "live_web_open_2026_06_10"
        receipt["source_freshness_status"] = "current_source_reverified_on_2026_06_10"
        receipt["access_caveat"] = (
            "Use as current pressure mapping only; do not infer unlisted production guarantees, "
            "benchmarked superiority, or Seraph parity from competitor docs."
        )
        receipt["competitor_claim_uncertainty"] = (
            "Docs and source pages can change after the access date; final wording remains bounded "
            "to named Seraph receipts and this audit's claim matrix."
        )
    return receipts


def parity_batch_reconciliation_receipts() -> list[dict[str, Any]]:
    completed_batches = [
        ("BV", 476, "production_parity_readiness", 484),
        ("BW", 477, "production_secure_host_hardening", 485),
        ("BX", 478, "production_durable_orchestration", 486),
        ("BY", 479, "production_reach_channel_hardening", 487),
        ("BZ", 480, "live_guardian_learning_quality", 488),
        ("CA", 481, "marketplace_grade_capability_lifecycle", 489),
        ("CB", 482, "production_operator_control_parity", 490),
        ("CC", 491, "live_external_orchestration_attestation", 498),
        ("CD", 492, "production_isolation_hardening_v2", 499),
        ("CE", 493, "live_broad_reach_channel_attestation", 500),
        ("CF", 494, "live_human_outcome_quality_study", 501),
        ("CG", 495, "third_party_marketplace_attestation", 502),
        ("CH", 496, "managed_browser_provider_attestation", 503),
        ("CJ", 505, "production_sla_orchestration", 514),
        ("CK", 506, "independent_secure_host_review", 515),
        ("CL", 509, "broad_channel_sla_operations", 516),
        ("CM", 507, "independent_outcome_cohort_review", 517),
        ("CN", 508, "long_work_debugging_recovery", 518),
        ("CO", 510, "independent_package_security_review", 519),
        ("CI", 497, FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME, 504),
        ("CP", 511, "live_browser_task_depth", 520),
        ("CQ", 512, FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME, 521),
    ]
    receipts = [
        {
            "batch": batch,
            "issue": issue,
            "primary_suite": suite,
            "merged_pr": pr,
            "status": "done",
            "project_fields_required": ["Queue", "Lane", "Priority", "Size", "Status", "Code Review", "PR"],
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "operator_visible": True,
        }
        for batch, issue, suite, pr in completed_batches
    ]
    return receipts


def claim_ledger_reconciliation_receipts() -> list[dict[str, Any]]:
    return [
        {
            "claim_id": "SCL-012",
            "area": "agent_parity_proof_train_status",
            "issue_links": [468, 475, 496, 497],
            "allowed_wording": "strategy artifact and proof train complete; full parity/exceedance remains incomplete",
            "blocked_claims": ["fully_at_parity", "reference_systems_exceeded"],
            "status": "partially_backed",
            "operator_surface": "/api/operator/final-parity-readiness-report",
        },
        {
            "claim_id": "SCL-026",
            "area": "production_sla_orchestration_and_recovery_evidence",
            "issue_links": [475, 505],
            "allowed_wording": (
                "production SLA orchestration, scoped effectively-once recovery, and duplicate side-effect "
                "audit receipts are visible"
            ),
            "blocked_claims": [
                "exactly_once_or_crash_proof_orchestration",
                "production_ready_product",
            ],
            "status": "backed_for_bounded_receipts_after_batch_cj_pr_merge",
            "operator_surface": "/api/operator/production-sla-orchestration",
        },
        {
            "claim_id": "SCL-027",
            "area": "independent_secure_host_review_and_isolation_hardening",
            "issue_links": [475, 506],
            "allowed_wording": (
                "independent secure-host review, live hostile drill, isolation evidence, and recovery "
                "authority receipts are visible"
            ),
            "blocked_claims": [
                "secure_private_by_default",
                "production_security_solved",
                "ironclaw_class_secure_execution",
                "tee_cvm_wasm_or_container_isolation_implemented",
                "production_ready_product",
            ],
            "status": "backed_for_bounded_receipts_after_batch_ck_pr_merge",
            "operator_surface": "/api/operator/independent-secure-host-review",
        },
        {
            "claim_id": "SCL-020",
            "area": "production_isolation_and_security_incident_proof",
            "issue_links": [492],
            "allowed_wording": "production-isolation and security-incident receipts are visible",
            "blocked_claims": ["secure_private_by_default", "ironclaw_class_secure_execution"],
            "status": "backed_for_bounded_receipts",
            "operator_surface": "/api/operator/production-isolation-hardening",
        },
        {
            "claim_id": "SCL-021",
            "area": "live_broad_reach_and_voice_media_proof",
            "issue_links": [493],
            "allowed_wording": "live broad reach and production voice/media proof receipts are visible",
            "blocked_claims": ["openclaw_class_reach", "voice_or_multimodal_parity"],
            "status": "backed_for_bounded_receipts",
            "operator_surface": "/api/operator/live-reach-media-proof",
        },
        {
            "claim_id": "SCL-022",
            "area": "live_human_outcome_and_causal_guardian_learning_proof",
            "issue_links": [494],
            "allowed_wording": "bounded recorded-live human-outcome and causal guardian-learning receipts are visible",
            "blocked_claims": ["guardian_intelligence_superiority", "memory_superiority"],
            "status": "backed_for_bounded_receipts",
            "operator_surface": "/api/operator/live-human-outcome-learning-proof",
        },
        {
            "claim_id": "SCL-023",
            "area": "live_marketplace_attestation_and_operations_proof",
            "issue_links": [495],
            "allowed_wording": "bounded recorded-live marketplace attestation and operations receipts are visible",
            "blocked_claims": ["production_secure_marketplace", "marketplace_superiority"],
            "status": "backed_for_bounded_receipts",
            "operator_surface": "/api/operator/live-marketplace-attestation-proof",
        },
        {
            "claim_id": "SCL-024",
            "area": "browser_provider_attestation_and_multi_operator_usability_proof",
            "issue_links": [496],
            "allowed_wording": "bounded browser-provider attestation and multi-operator usability receipts are visible",
            "blocked_claims": ["safe_browser_automation", "full_browser_parity", "best_cockpit"],
            "status": "backed_for_bounded_receipts",
            "operator_surface": "/api/operator/browser-provider-usability-proof",
        },
        {
            "claim_id": "SCL-025",
            "area": "final_source_backed_parity_readiness_audit",
            "issue_links": [475, 497],
            "allowed_wording": "final source-backed parity readiness audit receipts are visible",
            "blocked_claims": [
                "fully_at_parity",
                "reference_systems_exceeded",
                "production_ready_product",
            ],
            "status": "backed_for_final_audit_receipts",
            "operator_surface": "/api/operator/final-parity-readiness-report",
        },
        {
            "claim_id": "SCL-028",
            "area": "broad_reach_production_voice_media_and_mobile_execution",
            "issue_links": [475, 509],
            "allowed_wording": (
                "bounded broad-channel SLA, production voice/media quality-gate, and mobile execution "
                "continuity receipts are visible"
            ),
            "blocked_claims": [
                "openclaw_class_reach",
                "voice_or_multimodal_parity",
                "always_available_operation",
                "production_ready_product",
            ],
            "status": "backed_for_bounded_receipts_after_batch_cl_pr_merge",
            "operator_surface": "/api/operator/production-reach-voice-mobile",
        },
        {
            "claim_id": "SCL-029",
            "area": "independent_guardian_learning_outcomes_and_memory_parity_proof",
            "issue_links": [475, 507],
            "allowed_wording": (
                "bounded independent outcome cohort, task-scoped causal-learning, and memory-provider parity "
                "matrix receipts are visible after the Batch CM PR lands"
            ),
            "blocked_claims": [
                "guardian_intelligence_superiority",
                "solved_live_learning",
                "live_human_outcome_superiority",
                "memory_superiority",
                "full_memory_provider_parity",
                "production_ready_product",
            ],
            "status": "backed_for_bounded_receipts_after_batch_cm_pr_merge",
            "operator_surface": "/api/operator/independent-learning-memory-parity",
        },
        {
            "claim_id": "SCL-030",
            "area": "dense_long_work_operator_debugging_and_recovery_control",
            "issue_links": [475, 508],
            "allowed_wording": (
                "bounded dense long-work debugging, recovery-control, and independent usability/accessibility "
                "receipts are visible after the Batch CN PR lands"
            ),
            "blocked_claims": [
                "best_cockpit",
                "world_class_cockpit",
                "solved_operator_control",
                "production_ready_product",
                "full_production_parity",
            ],
            "status": "backed_for_bounded_receipts_after_batch_cn_pr_merge",
            "operator_surface": "/api/operator/dense-operator-recovery-control",
        },
        {
            "claim_id": "SCL-031",
            "area": "production_marketplace_security_and_package_network_operations",
            "issue_links": [475, 510],
            "allowed_wording": (
                "bounded independent package-security review, hostile ecosystem/package-network incident, "
                "publisher trust, vulnerability handling, rollback, and quarantine diagnostics receipts are visible "
                "after the Batch CO PR lands"
            ),
            "blocked_claims": [
                "production_secure_marketplace",
                "third_party_package_security_solved",
                "ecosystem_superiority",
                "full_marketplace_parity",
                "production_ready_product",
                "reference_systems_exceeded",
            ],
            "status": "backed_for_bounded_receipts_after_batch_co_pr_merge",
            "operator_surface": "/api/operator/production-marketplace-security",
        },
        {
            "claim_id": "SCL-032",
            "area": "safe_autonomous_browser_computer_use_and_full_browser_parity",
            "issue_links": [475, 511],
            "allowed_wording": (
                "bounded safe/test-account browser task-depth, autonomous safety-control, session-partition, "
                "site-recovery, provider-reliability, and independent-usability receipts are visible after "
                "the Batch CP PR lands"
            ),
            "blocked_claims": [
                "safe_browser_automation",
                "safe_autonomous_computer_use",
                "full_browser_parity",
                "production_ready_product",
                "full_production_parity",
                "reference_systems_exceeded",
            ],
            "status": "backed_for_bounded_receipts_after_batch_cp_pr_merge",
            "operator_surface": "/api/operator/safe-autonomous-browser-computer-use",
        },
        {
            "claim_id": "SCL-033",
            "area": "full_parity_claim_lift_and_final_critic_audit",
            "issue_links": [475, 512],
            "allowed_wording": (
                "Seraph has completed a board-backed parity proof train and final claim-lift audit "
                "with bounded receipts"
            ),
            "blocked_claims": [
                "fully_at_parity",
                "reference_systems_exceeded",
                "production_ready_product",
                "secure_private_by_default",
                "ironclaw_class_secure_execution",
                "openclaw_class_reach",
                "safe_browser_automation",
                "full_browser_parity",
                "production_secure_marketplace",
                "solved_operator_control",
                "guardian_intelligence_superiority",
                "memory_superiority",
            ],
            "status": "backed_for_bounded_final_claim_lift_receipts_broad_claims_continue_blocked",
            "operator_surface": "/api/operator/final-parity-readiness-report",
        },
    ]


def final_claim_lift_matrix() -> list[dict[str, Any]]:
    return [
        {
            "claim_id": "SCL-028",
            "batch": "CL",
            "issue": 509,
            "merged_pr": 516,
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "tests": ["tests/test_operator_api.py", "tests/test_final_parity_audit.py"],
            "operator_surface": "/api/operator/production-reach-voice-mobile",
            "evidence": [
                "broad_channel_sla_operations",
                "production_voice_media_quality_gates",
                "mobile_execution_continuity",
            ],
            "permitted_exact_wording": (
                "Seraph ships bounded broad-channel SLA, production voice/media quality-gate, "
                "and mobile-execution continuity receipts."
            ),
            "narrowed_wording": "OpenClaw-class reach, full voice/media parity, and always-available operation remain unproven.",
            "continued_blocked_claims": [
                "openclaw_class_reach",
                "voice_or_multimodal_parity",
                "always_available_operation",
                "production_ready_product",
                "fully_at_parity",
                "reference_systems_exceeded",
            ],
            "disposition": "narrowed",
        },
        {
            "claim_id": "SCL-029",
            "batch": "CM",
            "issue": 507,
            "merged_pr": 517,
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "tests": ["tests/test_operator_api.py", "tests/test_final_parity_audit.py"],
            "operator_surface": "/api/operator/independent-learning-memory-parity",
            "evidence": [
                "independent_outcome_cohort_review",
                "task_scoped_causal_learning",
                "memory_provider_parity_matrix",
            ],
            "permitted_exact_wording": (
                "Seraph ships bounded independent guardian-learning outcome and memory-provider "
                "parity-matrix receipts."
            ),
            "narrowed_wording": "Guardian intelligence superiority and memory superiority remain unproven.",
            "continued_blocked_claims": [
                "guardian_intelligence_superiority",
                "solved_live_learning",
                "live_human_outcome_superiority",
                "memory_superiority",
                "full_memory_provider_parity",
                "production_ready_product",
                "fully_at_parity",
                "reference_systems_exceeded",
            ],
            "disposition": "narrowed",
        },
        {
            "claim_id": "SCL-030",
            "batch": "CN",
            "issue": 508,
            "merged_pr": 518,
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "tests": ["tests/test_operator_api.py", "tests/test_final_parity_audit.py"],
            "operator_surface": "/api/operator/dense-operator-recovery-control",
            "evidence": [
                "long_work_debugging_recovery",
                "operator_control_density",
                "independent_operator_usability_accessibility",
            ],
            "permitted_exact_wording": (
                "Seraph ships bounded dense long-work operator debugging, recovery-control, "
                "and independent usability/accessibility receipts."
            ),
            "narrowed_wording": "Best cockpit, world-class cockpit, and solved operator control remain unproven.",
            "continued_blocked_claims": [
                "best_cockpit",
                "world_class_cockpit",
                "solved_operator_control",
                "production_ready_product",
                "fully_at_parity",
                "reference_systems_exceeded",
            ],
            "disposition": "narrowed",
        },
        {
            "claim_id": "SCL-031",
            "batch": "CO",
            "issue": 510,
            "merged_pr": 519,
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "tests": ["tests/test_operator_api.py", "tests/test_final_parity_audit.py"],
            "operator_surface": "/api/operator/production-marketplace-security",
            "evidence": [
                "independent_package_security_review",
                "hostile_ecosystem_package_drills",
                "package_network_incident_operations",
                "publisher_trust_vulnerability_handling",
                "marketplace_rollback_quarantine_diagnostics",
            ],
            "permitted_exact_wording": (
                "Seraph ships bounded independent package-security review, hostile ecosystem/package-network "
                "incident, publisher trust, vulnerability handling, rollback, and quarantine diagnostics receipts."
            ),
            "narrowed_wording": "Production-secure marketplace and solved third-party package security remain unproven.",
            "continued_blocked_claims": [
                "production_secure_marketplace",
                "third_party_package_security_solved",
                "ecosystem_superiority",
                "full_marketplace_parity",
                "production_ready_product",
                "fully_at_parity",
                "reference_systems_exceeded",
            ],
            "disposition": "narrowed",
        },
        {
            "claim_id": "SCL-032",
            "batch": "CP",
            "issue": 511,
            "merged_pr": 520,
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "tests": ["tests/test_safe_browser_computer_use.py", "tests/test_operator_api.py", "tests/test_final_parity_audit.py"],
            "operator_surface": "/api/operator/safe-autonomous-browser-computer-use",
            "evidence": [
                "live_browser_task_depth",
                "autonomous_browser_safety_controls",
                "browser_session_partitioning_security",
                "site_specific_recovery_drills",
                "browser_provider_reliability_matrix",
                "independent_browser_usability_review",
            ],
            "permitted_exact_wording": "Seraph ships bounded redacted browser/computer-use safety receipt evidence.",
            "narrowed_wording": "Blanket safe browser automation and full browser parity remain unproven.",
            "continued_blocked_claims": [
                "safe_browser_automation",
                "safe_autonomous_computer_use",
                "full_browser_parity",
                "production_ready_product",
                "fully_at_parity",
                "reference_systems_exceeded",
            ],
            "disposition": "narrowed",
        },
        {
            "claim_id": "SCL-033",
            "batch": "CQ",
            "issue": 512,
            "merged_pr": 521,
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "tests": ["tests/test_final_parity_audit.py", "tests/test_operator_api.py", "tests/test_strategy_claims.py"],
            "operator_surface": "/api/operator/final-parity-readiness-report",
            "evidence": [
                "current_competitor_source_receipts",
                "parity_batch_reconciliation_receipts",
                "final_claim_lift_matrix",
                "critic_disposition_receipts",
            ],
            "permitted_exact_wording": (
                "Seraph has completed a board-backed parity proof train and final claim-lift audit "
                "with bounded receipts."
            ),
            "currently_permitted_exact_wording": (
                "Seraph has completed a board-backed parity proof train and final claim-lift audit "
                "with bounded receipts."
            ),
            "currently_allowed": True,
            "narrowed_wording": (
                "This is proof-train completion, not product-wide full parity, production readiness, "
                "security superiority, or reference-system exceedance."
            ),
            "continued_blocked_claims": list(FINAL_PARITY_AUDIT_BLOCKED_CLAIMS),
            "disposition": "bounded_completion_wording_allowed_broad_claims_continue_blocked",
        },
    ]


def exact_stronger_claim_outcomes() -> list[dict[str, Any]]:
    claim_names = [
        ("fully_at_parity", "The board-backed parity proof train is complete; product-wide full parity remains blocked."),
        ("production_ready_product", "Named receipt surfaces exist; production-ready product wording remains blocked."),
        ("reference_systems_exceeded", "Targeted vision and proof train are documented; reference-system exceedance remains blocked."),
        ("secure_private_by_default", "Trust-boundary receipts exist; secure/private-by-default wording remains blocked."),
        ("ironclaw_class_secure_execution", "Bounded isolation receipts exist; IronClaw-class execution remains blocked."),
        ("openclaw_class_reach", "Reach receipts exist; OpenClaw-class reach remains blocked."),
        ("safe_browser_automation", "Browser/computer-use safety receipts exist; blanket safe browser automation remains blocked."),
        ("full_browser_parity", "Browser task and recovery receipts exist; full browser parity remains blocked."),
        ("production_secure_marketplace", "Package-security receipts exist; production-secure marketplace remains blocked."),
        ("solved_operator_control", "Dense control receipts exist; solved operator control remains blocked."),
        ("guardian_intelligence_superiority", "Learning receipts exist; guardian intelligence superiority remains blocked."),
        ("memory_superiority", "Memory-provider receipts exist; memory superiority remains blocked."),
    ]
    return [
        {
            "claim": claim,
            "outcome": "continued_blocked",
            "permitted_exact_wording": replacement,
            "requires_ledger_permission": True,
        }
        for claim, replacement in claim_names
    ]


def residual_gap_receipts() -> list[dict[str, Any]]:
    return [
        {
            "gap_id": "ci-gap-orchestration-sla",
            "area": "runtime_reliability",
            "gap": (
                "Batch CJ narrows the orchestration SLA gap with provider windows, jitter budgets, "
                "failure-injection receipts, and duplicate side-effect audit receipts; unconditional "
                "exactly-once and crash-proof guarantees remain unproven"
            ),
            "blocking_claims": ["exactly_once_or_crash_proof_orchestration", "production_ready_product"],
            "current_batch_evidence": [
                "production_sla_orchestration",
                "exactly_once_recovery_evidence",
                "duplicate_side_effect_audit",
                "/api/operator/production-sla-orchestration",
                "GitHub issue #505",
            ],
            "required_stronger_evidence": (
                "continuous independent production SLA monitoring and broader crash/failover proof before "
                "global exactly-once, crash-proof, or production-ready wording"
            ),
        },
        {
            "gap_id": "ci-gap-security-independent",
            "area": "trust_boundaries",
            "gap": (
                "Batch CK narrows the independent security gap with independent-review receipts, live "
                "hostile-drill receipts, isolation-evidence matrices, and recovery-authority controls; "
                "secure/private-by-default, IronClaw-class, and hardware-backed/container-grade isolation "
                "claims remain blocked"
            ),
            "blocking_claims": ["secure_private_by_default", "ironclaw_class_secure_execution"],
            "current_batch_evidence": [
                "independent_secure_host_review",
                "live_hostile_isolation_drills",
                "secure_host_recovery_authority",
                "/api/operator/independent-secure-host-review",
                "GitHub issue #506",
            ],
            "required_stronger_evidence": (
                "external certification or production penetration testing plus hardware-backed/container-grade "
                "isolation proof before secure/private, IronClaw-class, or production-ready wording"
            ),
        },
        {
            "gap_id": "ci-gap-reach-media-production",
            "area": "presence_and_reach",
            "gap": (
                "Batch CL narrows the reach/media/mobile gap with channel SLA, production voice/media "
                "quality-gate, mobile execution continuity, abuse/rate-limit, and coverage-gap receipts; "
                "OpenClaw-class reach, voice/media parity, always-available operation, and production-ready "
                "wording remain blocked"
            ),
            "blocking_claims": ["openclaw_class_reach", "voice_or_multimodal_parity"],
            "current_batch_evidence": [
                "broad_channel_sla_operations",
                "production_voice_media_quality_gates",
                "mobile_execution_continuity",
                "/api/operator/production-reach-voice-mobile",
                "GitHub issue #509",
            ],
            "required_stronger_evidence": (
                "larger independent channel/provider monitoring, broader real-world mobile operations, and "
                "current-source final audit before broad reach, voice/media parity, always-available, or "
                "production-ready wording"
            ),
        },
        {
            "gap_id": "ci-gap-human-outcomes-independent",
            "area": "guardian_intelligence",
            "gap": (
                "Batch CM narrows the independent learning/memory gap with independent outcome cohort, "
                "task-scoped causal-learning, privacy/rollback, and memory-provider parity-matrix receipts; "
                "guardian intelligence superiority, memory superiority, solved learning, live human-outcome "
                "superiority, and full memory-provider parity remain blocked"
            ),
            "blocking_claims": ["guardian_intelligence_superiority", "memory_superiority"],
            "current_batch_evidence": [
                "independent_outcome_cohort_review",
                "task_scoped_causal_learning",
                "memory_provider_parity_matrix",
                "/api/operator/independent-learning-memory-parity",
                "GitHub issue #507",
            ],
            "required_stronger_evidence": (
                "powered named-baseline comparisons, larger independent populations, and final claim-ledger review "
                "before guardian superiority, memory superiority, solved learning, or full memory-provider parity wording"
            ),
        },
        {
            "gap_id": "ci-gap-marketplace-security",
            "area": "ecosystem_and_leverage",
            "gap": (
                "Batch CO narrows the marketplace-security gap with independent package review, hostile "
                "ecosystem drills, package-network incident operations, publisher trust, vulnerability "
                "handling, and rollback/quarantine diagnostics; production-secure marketplace, solved "
                "third-party package security, full marketplace parity, and ecosystem superiority remain blocked"
            ),
            "blocking_claims": [
                "production_secure_marketplace",
                "third_party_package_security_solved",
                "full_marketplace_parity",
                "reference_systems_exceeded",
            ],
            "current_batch_evidence": [
                "independent_package_security_review",
                "hostile_ecosystem_package_drills",
                "package_network_incident_operations",
                "publisher_trust_vulnerability_handling",
                "marketplace_rollback_quarantine_diagnostics",
                "/api/operator/production-marketplace-security",
                "GitHub issue #510",
            ],
            "required_stronger_evidence": (
                "external marketplace security review, larger live package-network corpus, independent scanner "
                "and vulnerability-source monitoring, and final claim-lift audit before production-secure "
                "marketplace, solved package-security, full marketplace parity, or superiority wording"
            ),
        },
        {
            "gap_id": "ci-gap-dense-operator-control",
            "area": "operator_cockpit",
            "gap": (
                "Batch CN narrows the dense operator-control gap with long-work debugging, control-density, "
                "independent usability/accessibility, keyboard-only, cross-batch residual-risk, and recovery "
                "correctness receipts; best/world-class cockpit and solved operator-control wording remain blocked"
            ),
            "blocking_claims": ["best_cockpit", "solved_operator_control"],
            "current_batch_evidence": [
                "long_work_debugging_recovery",
                "operator_control_density",
                "independent_operator_usability_accessibility",
                "/api/operator/dense-operator-recovery-control",
                "GitHub issue #508",
            ],
            "required_stronger_evidence": (
                "final claim-ledger reconciliation and broader independent usability population evidence before "
                "best-cockpit or solved-control wording"
            ),
        },
        {
            "gap_id": "ci-gap-browser-autonomy",
            "area": "browser_computer_use",
            "gap": (
                "Batch CP narrows the browser-autonomy gap with bounded safe/test-account task depth, "
                "autonomous safety-control, session partitioning, site-specific recovery, provider reliability, "
                "and independent usability receipts; blanket safe browser automation, safe autonomous computer-use, "
                "full browser parity, production-ready, and full parity wording remain blocked pending final claim audit"
            ),
            "blocking_claims": [
                "safe_browser_automation",
                "safe_autonomous_computer_use",
                "full_browser_parity",
                "production_ready_product",
            ],
            "current_batch_evidence": [
                "live_browser_task_depth",
                "autonomous_browser_safety_controls",
                "browser_session_partitioning_security",
                "site_specific_recovery_drills",
                "browser_provider_reliability_matrix",
                "independent_browser_usability_review",
                "/api/operator/safe-autonomous-browser-computer-use",
                "GitHub issue #511",
            ],
            "required_stronger_evidence": (
                "Batch CQ final source-backed claim reconciliation plus broader production provider/site evidence "
                "before blanket safe automation, full browser parity, or production-ready wording"
            ),
        },
    ]


def critic_disposition_receipts() -> list[dict[str, Any]]:
    return [
        {
            "review_id": "ci-critic-current-source",
            "role": "Critic/Contrarian",
            "finding": "competitor claims are current-source-backed but should remain pressure mapping, not superiority evidence",
            "disposition": "accepted",
            "operator_visible": True,
        },
        {
            "review_id": "ci-critic-board-doc-sync",
            "role": "Critic/Contrarian",
            "finding": "Project/issue/doc reconciliation is required before closing #475 or #497",
            "disposition": "accepted",
            "operator_visible": True,
        },
        {
            "review_id": "ci-critic-false-completion",
            "role": "Critic/Contrarian",
            "finding": "residual gaps block full parity, superiority, and production-ready wording",
            "disposition": "accepted",
            "operator_visible": True,
        },
    ]


def build_final_parity_audit_contract() -> dict[str, Any]:
    sources = current_competitor_source_receipts()
    batches = parity_batch_reconciliation_receipts()
    claims = claim_ledger_reconciliation_receipts()
    claim_lift = final_claim_lift_matrix()
    exact_claims = exact_stronger_claim_outcomes()
    gaps = residual_gap_receipts()
    critic = critic_disposition_receipts()
    policy = final_parity_audit_policy_payload()
    completed_batches = [item for item in batches if item["status"] == "done"]
    return {
        "summary": {
            "operator_status": "final_parity_readiness_report_visible",
            "source_receipt_count": len(sources),
            "competitor_count": len({item["system"] for item in sources}),
            "current_source_date": "2026-06-10",
            "completed_batch_count": len(completed_batches),
            "final_batch_status": next(item["status"] for item in batches if item["batch"] == "CQ"),
            "claim_ledger_receipt_count": len(claims),
            "claim_lift_matrix_count": len(claim_lift),
            "exact_stronger_claim_count": len(exact_claims),
            "continued_blocked_stronger_claim_count": sum(
                1 for item in exact_claims if item["outcome"] == "continued_blocked"
            ),
            "residual_gap_count": len(gaps),
            "blocked_claim_count": len(policy["blocked_claims"]),
            "critic_disposition_count": len(critic),
            "all_sources_have_urls_and_dates": all(item.get("url") and item.get("checked_on") for item in sources),
            "all_sources_reachable_with_caveats": all(
                item.get("access_status") == "reachable"
                and item.get("access_caveat")
                and item.get("competitor_claim_uncertainty")
                for item in sources
            ),
            "all_completed_batches_done_merged_passed": all(
                item["project_status"] == "Done"
                and item["project_pr"] == "Merged"
                and item["code_review"] == "Passed"
                for item in completed_batches
            ),
            "all_claim_lift_rows_have_project_and_pr_evidence": all(
                item.get("project_status")
                and item.get("project_pr")
                and item.get("code_review")
                and item.get("operator_surface")
                and item.get("tests")
                and item.get("continued_blocked_claims")
                for item in claim_lift
            ),
            "bounded_parity_proof_train_completion_wording_allowed": True,
            "bounded_parity_proof_train_completion_wording_allowed_after_cq_merge": True,
            "full_parity_claim_allowed": False,
            "reference_systems_exceeded_claim_allowed": False,
            "claim_boundary": FINAL_PARITY_AUDIT_CLAIM_BOUNDARY,
        },
        "current_source_receipts": sources,
        "batch_reconciliation_receipts": batches,
        "claim_ledger_reconciliation": claims,
        "claim_lift_matrix": claim_lift,
        "exact_stronger_claim_outcomes": exact_claims,
        "residual_gap_receipts": gaps,
        "critic_disposition_receipts": critic,
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
            "summary": str(getattr(result, "error", "") or "Final parity audit scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_final_parity_audit_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME,
        FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
        OPERATOR_FINAL_PARITY_READINESS_REPORT_SUITE_NAME,
    ])


async def build_final_parity_readiness_report() -> dict[str, Any]:
    summary = await _run_final_parity_audit_suites()
    contract = build_final_parity_audit_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "final_parity_audit_ci_gated_operator_visible"
                if healthy
                else "final_parity_audit_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES)
                + len(FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES)
                + len(OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME: list(
                FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES
            ),
            FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME: list(
                FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES
            ),
            OPERATOR_FINAL_PARITY_READINESS_REPORT_SUITE_NAME: list(
                OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="final_parity_audit"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def post_cq_claim_readiness_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            POST_CQ_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
            REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SUITE_NAME,
            FALSE_COMPLETION_SCAN_V2_SUITE_NAME,
        ],
        "claim_boundary": POST_CQ_CLAIM_READINESS_CLAIM_BOUNDARY,
        "allowed_wording": POST_CQ_CLAIM_READINESS_ALLOWED_WORDING,
        "source_policy": (
            "post-CQ competitor-dependent wording requires official/source URLs checked on 2026-06-11, "
            "pressure-axis mapping, access caveats, and explicit stale-source uncertainty"
        ),
        "board_policy": (
            "Batch CZ reconciles parent #475 and batches #522-#530 without creating duplicate parent issues "
            "or mutating the historical CQ final-audit contract"
        ),
        "claim_policy": (
            "only the exact post-CQ bounded receipt wording is allowed; full parity, production readiness, "
            "security, reach, browser, marketplace, operator-control, guardian-superiority, memory-superiority, "
            "and reference-system-exceeded wording remains blocked"
        ),
        "receipt_surfaces": [
            "/api/operator/post-cq-claim-readiness",
            "/api/operator/benchmark-proof",
            "/api/operator/final-parity-readiness-report",
            "/api/operator/continuous-orchestration-slo",
            "/api/operator/container-grade-secure-host",
            "/api/operator/broad-reach-field-ops",
            "/api/operator/longitudinal-guardian-outcomes",
            "/api/operator/operator-control-population-study",
            "/api/operator/marketplace-security-corpus",
            "/api/operator/browser-computer-use-parity-depth",
            "docs/research/19-strategy-claim-ledger.md",
            "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
            "docs/implementation/16-agent-parity-execution-roadmap.md",
            "docs/implementation/09-benchmark-status.md",
        ],
        "blocked_claims": list(POST_CQ_CLAIM_READINESS_BLOCKED_CLAIMS),
        "not_claimed": [
            "full_product_parity",
            "reference_systems_exceeded",
            "production_ready",
            "ironclaw_class_secure_execution",
            "openclaw_class_reach",
            "safe_autonomous_browser_computer_use",
            "production_secure_marketplace",
        ],
    }


def reference_system_source_refresh_v2_receipts() -> list[dict[str, Any]]:
    receipts = current_competitor_source_receipts()
    source_line_receipts = {
        "hermes-features-overview": "lines_45_82_cover_memory_context_delegation_browser_voice_integrations",
        "hermes-tools-toolsets": "lines_61_82_cover_tool_registry_categories_and_gateway",
        "openclaw-control-ui": "docs_navigation_confirms_gateway_ops_control_surface",
        "openclaw-browser": "lines_63_69_and_462_482_cover_profiles_tabs_snapshots_ssrf_and_control_api",
        "openclaw-plugins": "lines_57_93_cover_plugin_capability_scope_install_sources_allow_deny",
        "ironclaw-security-site": "lines_40_160_cover_tee_vault_allowlist_wasm_and_near_ai_cloud_pressure",
        "ironclaw-feature-parity-matrix": "lines_0_3_cover_feature_matrix_and_2026_03_10_review_date",
    }
    for receipt in receipts:
        receipt["checked_on"] = "2026-06-11"
        receipt["accessed_on"] = "2026-06-11"
        receipt["verification_method"] = "static_snapshot_external_critic_reachability_review_2026_06_11"
        receipt["runtime_fetch_performed"] = False
        receipt["external_reachability_receipt"] = (
            "Independent Critic/Contrarian Rawls reported the named source URLs reachable on 2026-06-11; "
            "the deterministic eval does not perform network fetches."
        )
        receipt["source_refresh_version"] = "v2_post_cq"
        receipt["source_freshness_status"] = "external_critic_reachability_reviewed_on_2026_06_11"
        receipt["evidence_locator"] = source_line_receipts.get(receipt["source_id"], "current_source_opened")
        receipt["claim_lift_allowed"] = False
    return receipts


def post_cq_batch_reconciliation_receipts() -> list[dict[str, Any]]:
    completed_batches = [
        ("CR", 522, "post_cq_shipped_truth_and_board_baseline_reconciliation", 531),
        ("CS", 523, "continuous_orchestration_slo_monitor", 532),
        ("CT", 524, "container_grade_capability_isolation", 533),
        ("CU", 525, "broad_reach_field_operations", 534),
        ("CV", 526, "longitudinal_guardian_outcome_study", 535),
        ("CW", 527, "operator_control_population_study", 536),
        ("CX", 528, "marketplace_security_corpus_v1", 537),
        ("CY", 529, "browser_task_breadth_matrix", 538),
    ]
    receipts = [
        {
            "batch": batch,
            "issue": issue,
            "primary_suite": suite,
            "merged_pr": pr,
            "status": "done",
            "project_fields_required": ["Queue", "Lane", "Priority", "Size", "Status", "Code Review", "PR"],
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "reconciliation_mode": "static_issue_pr_receipt_snapshot",
            "live_project_verification_required": True,
            "operator_visible": True,
        }
        for batch, issue, suite, pr in completed_batches
    ]
    receipts.append({
        "batch": "CZ",
        "issue": 530,
        "primary_suite": POST_CQ_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
        "merged_pr": None,
        "closing_pr": "GitHub linked PR is authoritative after creation",
        "status": "cz_gate_receipts_visible",
        "project_fields_required": ["Queue", "Lane", "Priority", "Size", "Status", "Code Review", "PR"],
        "project_status": "GitHub Project is authoritative for live PR state",
        "project_pr": "GitHub Project is authoritative for live PR state",
        "code_review": "GitHub Project is authoritative for live review state",
        "reconciliation_mode": "external_github_project_workflow_required",
        "live_project_verification_required": True,
        "operator_visible": True,
    })
    return receipts


def post_cq_claim_ledger_reconciliation_receipts() -> list[dict[str, Any]]:
    rows = [
        ("SCL-034", "continuous_orchestration_slo_and_recovery_operations", 523, "/api/operator/continuous-orchestration-slo"),
        ("SCL-035", "container_grade_secure_host_validation", 524, "/api/operator/container-grade-secure-host"),
        ("SCL-036", "broad_reach_field_and_voice_media_operations", 525, "/api/operator/broad-reach-field-ops"),
        ("SCL-037", "longitudinal_guardian_learning_and_memory_outcomes", 526, "/api/operator/longitudinal-guardian-outcomes"),
        ("SCL-038", "dense_operator_mission_control_and_long_work_debugging", 527, "/api/operator/operator-control-population-study"),
        ("SCL-039", "marketplace_registry_corpus_and_continuous_security_operations", 528, "/api/operator/marketplace-security-corpus"),
        ("SCL-040", "managed_browser_computer_use_parity_depth", 529, "/api/operator/browser-computer-use-parity-depth"),
        ("SCL-041", "post_cq_final_production_evidence_claim_readiness", 530, "/api/operator/post-cq-claim-readiness"),
    ]
    receipts = []
    for claim_id, area, issue, surface in rows:
        receipts.append({
            "claim_id": claim_id,
            "area": area,
            "issue_links": [475, issue],
            "operator_surface": surface,
            "allowed_wording": (
                POST_CQ_CLAIM_READINESS_ALLOWED_WORDING
                if claim_id == "SCL-041"
                else "bounded post-CQ production-evidence receipts are visible"
            ),
            "blocked_claims": list(POST_CQ_CLAIM_READINESS_BLOCKED_CLAIMS),
            "status": (
                "backed_for_post_cq_bounded_claim_readiness_receipts"
                if claim_id == "SCL-041"
                else "backed_for_bounded_post_cq_receipts_broad_claims_continue_blocked"
            ),
        })
    return receipts


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _scan_false_completion_scope(globs: list[str]) -> dict[str, Any]:
    root = _repo_root()
    scanned_files: set[str] = set()
    violations: list[dict[str, Any]] = []
    for pattern in globs:
        for path in root.glob(pattern):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative_path = path.relative_to(root).as_posix()
            scanned_files.add(relative_path)
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), start=1):
                lower_line = line.lower()
                is_claim_ledger_control_row = (
                    relative_path == "docs/research/19-strategy-claim-ledger.md"
                    and (line.startswith("| `SCL-") or line.startswith("| \""))
                )
                is_scanner_forbidden_phrase_constant = (
                    relative_path == "backend/src/evals/final_parity_audit.py"
                    and line.strip().startswith("\"")
                    and any(
                        phrase.lower() in lower_line
                        for phrase in POST_CQ_FALSE_COMPLETION_FORBIDDEN_PHRASES
                    )
                )
                if is_claim_ledger_control_row or is_scanner_forbidden_phrase_constant:
                    continue
                has_allowed_context = any(
                    marker in lower_line
                    for marker in POST_CQ_FALSE_COMPLETION_ALLOWED_CONTEXT_MARKERS
                )
                if has_allowed_context:
                    continue
                for phrase in POST_CQ_FALSE_COMPLETION_FORBIDDEN_PHRASES:
                    if phrase.lower() in lower_line:
                        violations.append({
                            "file": relative_path,
                            "line": line_number,
                            "phrase": phrase,
                        })
    return {
        "files_scanned": sorted(scanned_files),
        "violations": violations,
        "violations_found": len(violations),
    }


def false_completion_scan_v2_receipts() -> list[dict[str, Any]]:
    docs_scope = [
        "README.md",
        "SECURITY.md",
        "SUPPORT.md",
        "docs/research/**/*.md",
        "docs/implementation/**/*.md",
        "docs/docs/**/*.md",
    ]
    code_operator_scope = [
        "backend/src/api/operator.py",
        "backend/src/evals/**/*.py",
        "backend/src/extensions/**/*.py",
        "backend/src/security/**/*.py",
        "backend/src/guardian/**/*.py",
        "backend/src/memory/**/*.py",
        "backend/src/cockpit/**/*.py",
        "frontend/src/**/*.ts",
        "frontend/src/**/*.tsx",
        "backend/tests/**/*.py",
        "scripts/**/*.py",
    ]
    allowed_contexts = [
        "claim ledger blocked-claim rows",
        "benchmark suite identifiers",
        "route names that include historical parity language",
        "explicit target or aspirational wording",
        "negative assertions that stronger claims remain blocked",
    ]
    docs_scan = _scan_false_completion_scope(docs_scope)
    code_scan = _scan_false_completion_scope(code_operator_scope)
    return [
        {
            "scan_id": "post-cq-docs-false-completion-scan",
            "scan_mode": "local_repository_file_scan",
            "scope": docs_scope,
            "files_scanned_count": len(docs_scan["files_scanned"]),
            "forbidden_phrases": list(POST_CQ_FALSE_COMPLETION_FORBIDDEN_PHRASES),
            "allowed_contexts": allowed_contexts,
            "violations_found": docs_scan["violations_found"],
            "violations": docs_scan["violations"],
            "replacement_rule": "use exact SCL-041 bounded receipt wording or a blocked-claim sentence",
        },
        {
            "scan_id": "post-cq-code-operator-false-completion-scan",
            "scan_mode": "local_repository_file_scan",
            "scope": code_operator_scope,
            "files_scanned_count": len(code_scan["files_scanned"]),
            "forbidden_phrases": list(POST_CQ_FALSE_COMPLETION_FORBIDDEN_PHRASES),
            "allowed_contexts": allowed_contexts,
            "violations_found": code_scan["violations_found"],
            "violations": code_scan["violations"],
            "replacement_rule": "operator payloads must expose booleans showing broad claims remain false",
        },
        {
            "scan_id": "post-cq-github-false-completion-scan",
            "scan_mode": "external_github_pr_issue_review_required",
            "scope": [
                "GitHub issue #530 completion receipt",
                "GitHub parent issue #475 disposition comment",
                "CZ PR body before ready-for-review",
                "GitHub Project fields for #530",
            ],
            "runtime_static_scan": False,
            "violations_found": None,
            "external_scan_status": "required_before_pr_creation_or_merge",
            "replacement_rule": "PR/issue bodies must distinguish audit completion from product-wide parity",
            "external_authority": "PR body, issue comments, and GitHub Project fields are checked during PR creation/merge workflow",
        },
    ]


def post_cq_critic_disposition_receipts() -> list[dict[str, Any]]:
    return [
        {
            "review_id": "rawls-cz-critic-source-refresh-static-boundary",
            "role": "Critic/Contrarian",
            "reviewer": "Rawls",
            "review_agent_id": "019eb3b1-8ea1-7832-8e65-7af456350d08",
            "finding": "source reachability was self-attested by the deterministic contract instead of fetched",
            "disposition": "accepted_fixed",
            "resolution": "source receipts now declare static snapshot mode, no runtime fetch, and external critic reachability review",
            "operator_visible": True,
        },
        {
            "review_id": "rawls-cz-critic-github-scan-boundary",
            "role": "Critic/Contrarian",
            "reviewer": "Rawls",
            "review_agent_id": "019eb3b1-8ea1-7832-8e65-7af456350d08",
            "finding": "GitHub issue/PR false-completion scan is external and must not be counted as a local passing scan",
            "disposition": "accepted_fixed",
            "resolution": "summary separates local clean scans from the required external GitHub workflow scan",
            "operator_visible": True,
        },
        {
            "review_id": "rawls-cz-critic-board-reconciliation-boundary",
            "role": "Critic/Contrarian",
            "reviewer": "Rawls",
            "review_agent_id": "019eb3b1-8ea1-7832-8e65-7af456350d08",
            "finding": "Project reconciliation is a static receipt snapshot unless GitHub Project is queried in the PR workflow",
            "disposition": "accepted_fixed",
            "resolution": "batch receipts now expose static snapshot mode plus live Project verification requirement",
            "operator_visible": True,
        },
        {
            "review_id": "rawls-cz-critic-doc-wording-boundary",
            "role": "Critic/Contrarian",
            "reviewer": "Rawls",
            "review_agent_id": "019eb3b1-8ea1-7832-8e65-7af456350d08",
            "finding": "docs were ahead of evidence when they implied source refresh, false-completion scan, board reconciliation, and critic disposition were all locally proven",
            "disposition": "accepted_fixed",
            "resolution": "docs and receipts now distinguish deterministic local evidence from external workflow gates",
            "operator_visible": True,
        },
    ]


def build_post_cq_claim_readiness_contract() -> dict[str, Any]:
    sources = reference_system_source_refresh_v2_receipts()
    batches = post_cq_batch_reconciliation_receipts()
    claims = post_cq_claim_ledger_reconciliation_receipts()
    scans = false_completion_scan_v2_receipts()
    critic = post_cq_critic_disposition_receipts()
    policy = post_cq_claim_readiness_policy_payload()
    completed_batches = [item for item in batches if item["status"] == "done"]
    local_scans = [item for item in scans if item.get("scan_mode") == "local_repository_file_scan"]
    external_scans = [item for item in scans if item.get("scan_mode") == "external_github_pr_issue_review_required"]
    local_false_completion_violation_count = sum(
        int(item["violations_found"])
        for item in local_scans
        if isinstance(item.get("violations_found"), int)
    )
    return {
        "summary": {
            "operator_status": "post_cq_claim_readiness_visible",
            "source_receipt_count": len(sources),
            "competitor_count": len({item["system"] for item in sources}),
            "current_source_date": "2026-06-11",
            "completed_post_cq_batch_count": len(completed_batches),
            "post_cq_batch_count": len(batches),
            "cz_batch_status": next(item["status"] for item in batches if item["batch"] == "CZ"),
            "claim_ledger_receipt_count": len(claims),
            "false_completion_scan_count": len(scans),
            "local_false_completion_violation_count": local_false_completion_violation_count,
            "false_completion_violation_count": local_false_completion_violation_count,
            "all_local_false_completion_scans_clean": all(item.get("violations_found") == 0 for item in local_scans),
            "false_completion_external_tracking_scan_required": any(
                item.get("scan_mode") == "external_github_pr_issue_review_required"
                for item in scans
            ),
            "false_completion_external_tracking_scan_pending": any(
                item.get("external_scan_status") == "required_before_pr_creation_or_merge"
                for item in external_scans
            ),
            "false_completion_public_claim_gate_clear": False,
            "critic_disposition_count": len(critic),
            "all_sources_have_urls_and_dates": all(item.get("url") and item.get("checked_on") for item in sources),
            "all_sources_static_snapshot_no_runtime_fetch": all(
                item.get("runtime_fetch_performed") is False for item in sources
            ),
            "all_sources_have_external_critic_reachability_receipts": all(
                item.get("external_reachability_receipt") for item in sources
            ),
            "all_sources_reachable_with_caveats": all(
                item.get("access_status") == "reachable"
                and item.get("access_caveat")
                and item.get("competitor_claim_uncertainty")
                for item in sources
            ),
            "all_completed_post_cq_batches_done_merged_passed": all(
                item["project_status"] == "Done"
                and item["project_pr"] == "Merged"
                and item["code_review"] == "Passed"
                for item in completed_batches
            ),
            "live_project_verification_required": any(
                item.get("live_project_verification_required") for item in batches
            ),
            "post_cq_bounded_claim_readiness_wording_allowed": True,
            "post_cq_bounded_claim_readiness_allowed_wording": POST_CQ_CLAIM_READINESS_ALLOWED_WORDING,
            "full_parity_claim_allowed": False,
            "reference_systems_exceeded_claim_allowed": False,
            "production_ready_claim_allowed": False,
            "secure_private_by_default_claim_allowed": False,
            "claim_boundary": POST_CQ_CLAIM_READINESS_CLAIM_BOUNDARY,
        },
        "reference_system_source_refresh_v2": sources,
        "post_cq_batch_reconciliation_receipts": batches,
        "post_cq_claim_ledger_reconciliation": claims,
        "false_completion_scan_v2": scans,
        "critic_disposition_receipts": critic,
        "policy": policy,
    }


async def _run_post_cq_claim_readiness_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_CQ_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME,
        REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SUITE_NAME,
        FALSE_COMPLETION_SCAN_V2_SUITE_NAME,
    ])


async def build_post_cq_claim_readiness_report() -> dict[str, Any]:
    summary = await _run_post_cq_claim_readiness_suites()
    contract = build_post_cq_claim_readiness_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "post_cq_claim_readiness_ci_gated_operator_visible"
                if healthy
                else "post_cq_claim_readiness_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(POST_CQ_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES)
                + len(REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SCENARIO_NAMES)
                + len(FALSE_COMPLETION_SCAN_V2_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            POST_CQ_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME: list(
                POST_CQ_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES
            ),
            REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SUITE_NAME: list(
                REFERENCE_SYSTEM_SOURCE_REFRESH_V2_SCENARIO_NAMES
            ),
            FALSE_COMPLETION_SCAN_V2_SUITE_NAME: list(FALSE_COMPLETION_SCAN_V2_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="post_cq_claim_readiness"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def final_production_parity_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_READINESS_SOAK_V1_SUITE_NAME,
            FINAL_FULL_PARITY_CLAIM_LIFT_V1_SUITE_NAME,
            REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SUITE_NAME,
            FALSE_COMPLETION_SCAN_V3_SUITE_NAME,
            BOARD_PR_ISSUE_RECONCILIATION_V3_SUITE_NAME,
        ],
        "claim_boundary": FINAL_PRODUCTION_PARITY_CLAIM_BOUNDARY,
        "allowed_wording": FINAL_PRODUCTION_PARITY_ALLOWED_WORDING,
        "source_policy": (
            "final production-parity wording requires current official/source-backed Hermes, OpenClaw, "
            "and IronClaw URLs reviewed on 2026-06-11 with manual-source caveats and pressure-only claim use; "
            "these receipts are not runtime fetch proof"
        ),
        "board_policy": (
            "Batch DH reconciles #475 and #540-#547 after #555 merged, closes stale roadmap PR #548, "
            "and keeps broad product claims blocked unless the claim ledger permits exact wording"
        ),
        "claim_policy": (
            "the final gate may expose bounded DA-DG production-readiness soak-readiness reconciliation receipts, "
            "but it does not "
            "by itself permit full parity, production readiness, superiority, secure/private, OpenClaw-class, "
            "IronClaw-class, safe-browser, solved-control, or marketplace-superiority wording"
        ),
        "receipt_surfaces": [
            "/api/operator/final-production-parity",
            "/api/operator/benchmark-proof",
            "/api/operator/post-cq-claim-readiness",
            "/api/operator/production-workflow-guarantees",
            "/api/operator/certified-secure-host",
            "/api/operator/always-available-reach-media",
            "/api/operator/generalized-guardian-outcomes",
            "/api/operator/operator-control-certification",
            "/api/operator/production-secure-marketplace",
            "/api/operator/full-browser-parity",
            "docs/research/19-strategy-claim-ledger.md",
            "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
            "docs/implementation/16-agent-parity-execution-roadmap.md",
            "docs/implementation/09-benchmark-status.md",
        ],
        "blocked_claims": list(FINAL_PRODUCTION_PARITY_BLOCKED_CLAIMS),
        "not_claimed": [
            "full_product_parity",
            "reference_systems_exceeded",
            "production_ready",
            "secure_private_by_default",
            "ironclaw_class_secure_execution",
            "openclaw_class_reach",
            "safe_autonomous_browser_computer_use",
            "production_secure_marketplace",
        ],
    }


def reference_system_source_refresh_v3_receipts() -> list[dict[str, Any]]:
    receipts = reference_system_source_refresh_v2_receipts()
    evidence_locators = {
        "hermes-features-overview": "lines_45_82_cover_memory_delegation_browser_voice_provider_routing_plugins",
        "hermes-tools-toolsets": "lines_61_82_cover_web_terminal_browser_media_delegation_memory_cron_delivery_mcp",
        "openclaw-control-ui": "control_ui_docs_cover_pairing_runtime_config_history_pwa_push_auth_media_debugging",
        "openclaw-browser": "browser_docs_cover_managed_profiles_remote_cdp_existing_session_and_secret_bearing_paths",
        "openclaw-plugins": "plugin_docs_cover_channel_model_agent_tool_skill_speech_media_web_extension_points",
        "ironclaw-security-site": "official_site_covers_vaults_endpoint_allowlists_wasm_tee_rust_and_leak_detection",
        "ironclaw-feature-parity-matrix": "staging_matrix_reviewed_2026_03_10_covers_control_ui_channels_api_diagnostics_extensions",
    }
    for receipt in receipts:
        receipt["checked_on"] = "2026-06-11"
        receipt["accessed_on"] = "2026-06-11"
        receipt["verification_method"] = "manual_team_lead_web_review_2026_06_11"
        receipt["runtime_fetch_performed"] = False
        receipt["source_refresh_kind"] = "manual_current_source_review_receipt"
        receipt["source_refresh_version"] = "v3_final_production_parity_gate"
        receipt["source_freshness_status"] = "manual_sources_reopened_on_2026_06_11"
        receipt["evidence_locator"] = evidence_locators.get(receipt["source_id"], "current_source_opened")
        receipt["claim_lift_allowed"] = False
        receipt["claim_use"] = "current_source_pressure_only"
        receipt["access_caveat"] = (
            "Source was manually reviewed during the 2026-06-11 Batch DH review; use only for pressure "
            "mapping and stale-source guardrails, not as proof of Seraph parity or superiority."
        )
    return receipts


def final_da_dg_batch_reconciliation_receipts() -> list[dict[str, Any]]:
    completed_batches = [
        ("DA", 540, "production_workflow_state_machine_v1", 549, "/api/operator/production-workflow-guarantees"),
        ("DB", 541, "runtime_isolation_implementation_v1", 550, "/api/operator/certified-secure-host"),
        ("DC", 542, "always_available_reach_operations_v1", 551, "/api/operator/always-available-reach-media"),
        ("DD", 543, "generalized_guardian_outcome_study_v1", 552, "/api/operator/generalized-guardian-outcomes"),
        ("DE", 544, "operator_control_certification_v1", 553, "/api/operator/operator-control-certification"),
        ("DF", 545, "production_secure_marketplace_v1", 554, "/api/operator/production-secure-marketplace"),
        ("DG", 546, "safe_autonomous_browser_runtime_v1", 555, "/api/operator/full-browser-parity"),
    ]
    receipts = [
        {
            "batch": batch,
            "issue": issue,
            "primary_suite": suite,
            "operator_surface": surface,
            "merged_pr": pr,
            "status": "done",
            "project_status": "Done",
            "project_pr": "Merged",
            "code_review": "Passed",
            "project_fields_required": ["Queue", "Lane", "Priority", "Size", "Status", "Code Review", "PR"],
            "live_project_verification": "verified_before_batch_dh_branch_start",
            "operator_visible": True,
        }
        for batch, issue, suite, pr, surface in completed_batches
    ]
    receipts.append({
        "batch": "DH",
        "issue": 547,
        "primary_suite": PRODUCTION_READINESS_SOAK_V1_SUITE_NAME,
        "operator_surface": "/api/operator/final-production-parity",
        "merged_pr": None,
        "status": "in_progress_on_feature_branch",
        "project_status": "In Progress",
        "project_pr": "Not Ready",
        "code_review": "Not Ready",
        "active_branch": "feat/batch-dh-final-production-parity",
        "project_fields_required": ["Queue", "Lane", "Priority", "Size", "Status", "Code Review", "PR"],
        "live_project_verification": "verified_when_batch_dh_started",
        "operator_visible": True,
    })
    return receipts


def production_readiness_soak_v1_receipts() -> list[dict[str, Any]]:
    rows = [
        ("runtime_reliability", "DA", "production_workflow_state_machine_v1", "/api/operator/production-workflow-guarantees"),
        ("trust_boundaries", "DB", "credential_broker_egress_enforcement_v1", "/api/operator/certified-secure-host"),
        ("presence_and_reach", "DC", "always_available_reach_operations_v1", "/api/operator/always-available-reach-media"),
        ("guardian_intelligence", "DD", "generalized_guardian_outcome_study_v1", "/api/operator/generalized-guardian-outcomes"),
        ("operator_control", "DE", "operator_control_certification_v1", "/api/operator/operator-control-certification"),
        ("ecosystem_and_marketplace", "DF", "production_secure_marketplace_v1", "/api/operator/production-secure-marketplace"),
        ("browser_computer_use", "DG", "safe_autonomous_browser_runtime_v1", "/api/operator/full-browser-parity"),
    ]
    return [
        {
            "area": area,
            "batch": batch,
            "primary_suite": suite,
            "operator_surface": surface,
            "soak_window": "final_batch_dh_representative_receipt_window",
            "evidence_mode": "representative_cross_surface_reconciliation",
            "actual_runtime_soak_performed": False,
            "operational_window": "not_a_live_soak_window",
            "sample_count": None,
            "failure_counter_source": "upstream_batch_receipts_only",
            "raw_receipt_handle": f"operator-dh:{batch.lower()}:{suite}",
            "raw_receipt_digest": _stable_digest({
                "area": area,
                "batch": batch,
                "suite": suite,
                "surface": surface,
            }),
            "residual_risk": (
                "soak_readiness_reconciliation_only_not_product_wide_production_ready_claim"
            ),
            "claim_lift_allowed": False,
            "operator_recovery_visible": True,
            "aggregate_benchmark_visible": True,
        }
        for area, batch, suite, surface in rows
    ]


def _stable_digest(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def final_full_parity_claim_lift_v1_receipts() -> list[dict[str, Any]]:
    rows = [
        ("SCL-043", "DA", 540, "/api/operator/production-workflow-guarantees"),
        ("SCL-044", "DB", 541, "/api/operator/certified-secure-host"),
        ("SCL-045", "DC", 542, "/api/operator/always-available-reach-media"),
        ("SCL-046", "DD", 543, "/api/operator/generalized-guardian-outcomes"),
        ("SCL-047", "DE", 544, "/api/operator/operator-control-certification"),
        ("SCL-048", "DF", 545, "/api/operator/production-secure-marketplace"),
        ("SCL-049", "DG", 546, "/api/operator/full-browser-parity"),
        ("SCL-050", "DH", 547, "/api/operator/final-production-parity"),
    ]
    receipts = []
    for claim_id, batch, issue, surface in rows:
        receipts.append({
            "claim_id": claim_id,
            "batch": batch,
            "issue": issue,
            "operator_surface": surface,
            "allowed_wording": (
                FINAL_PRODUCTION_PARITY_ALLOWED_WORDING
                if claim_id == "SCL-050"
                else "bounded DA-DG evidence receipts are visible"
            ),
            "claim_lift_allowed": claim_id == "SCL-050",
            "broad_claim_lift_allowed": False,
            "blocked_claims": list(FINAL_PRODUCTION_PARITY_BLOCKED_CLAIMS),
            "disposition": "bounded_wording_only_broad_claims_blocked",
        })
    return receipts


def false_completion_scan_v3_receipts() -> list[dict[str, Any]]:
    scans = false_completion_scan_v2_receipts()
    local_scans = [item for item in scans if item.get("scan_mode") == "local_repository_file_scan"]
    return [
        {
            **item,
            "scan_id": item["scan_id"].replace("post-cq", "batch-dh"),
            "replacement_rule": "use exact SCL-050 bounded receipt wording or a blocked-claim sentence",
        }
        for item in local_scans
    ] + [
        {
            "scan_id": "batch-dh-github-tracking-false-completion-scan",
            "scan_mode": "external_github_pr_issue_review_performed",
            "scope": [
                "parent issue #475",
                "batch issue #546 after PR #555 merge",
                "batch issue #547 active branch receipt",
                "stale roadmap PR #548 closure",
                "Batch DH PR body before merge",
            ],
            "runtime_static_scan": False,
            "violations_found": 0,
            "external_scan_status": "performed_after_issue_475_body_refresh",
            "issue_475_body_refreshed": True,
            "replacement_rule": "issues and PRs distinguish bounded receipts from product-wide parity",
        },
        {
            "scan_id": "batch-dh-stale-pr-closure-scan",
            "scan_mode": "github_pr_state_receipt",
            "scope": ["PR #548"],
            "stale_pr_number": 548,
            "stale_pr_state": "CLOSED",
            "stale_pr_reason": "superseded_dirty_branch_would_delete_merged_da_df_code",
            "violations_found": 0,
        },
    ]


def final_production_parity_critic_receipts() -> list[dict[str, Any]]:
    return [
        {
            "review_id": "dh-external-critic-evidence-quality",
            "role": "Critic/Contrarian",
            "finding": (
                "source refresh, production-readiness soak, and GitHub tracking scans must not be represented "
                "as runtime fetch proof, live soak evidence, or clean external state while stale issue text remains"
            ),
            "disposition": "accepted_fixed",
            "resolution": (
                "downgraded source receipts to manual review, marked soak-readiness receipts as representative "
                "reconciliation only, and refreshed #475 DA-DH issue-body rows"
            ),
            "operator_visible": True,
        },
        {
            "review_id": "dh-local-critic-stale-pr-548",
            "role": "Critic/Contrarian",
            "finding": "PR #548 was stale and dirty; keeping it open made #475 board state falsely show active review",
            "disposition": "accepted_fixed",
            "resolution": "closed #548 as superseded and reset #475 PR and Code Review fields to Not Ready",
            "operator_visible": True,
        },
        {
            "review_id": "dh-critic-no-duplicate-parent",
            "role": "Critic/Contrarian",
            "finding": "creating a second full-parity parent would duplicate #475",
            "disposition": "accepted",
            "resolution": "reused #475 and #547 as the active final gate",
            "operator_visible": True,
        },
        {
            "review_id": "dh-critic-claim-boundary",
            "role": "Critic/Contrarian",
            "finding": "Batch DH must not imply production readiness or full parity from bounded DA-DG receipts",
            "disposition": "accepted",
            "resolution": "operator report exposes bounded allowed wording and keeps broad claims blocked",
            "operator_visible": True,
        },
    ]


def build_final_production_parity_contract() -> dict[str, Any]:
    sources = reference_system_source_refresh_v3_receipts()
    batches = final_da_dg_batch_reconciliation_receipts()
    soak = production_readiness_soak_v1_receipts()
    claim_lift = final_full_parity_claim_lift_v1_receipts()
    scans = false_completion_scan_v3_receipts()
    critic = final_production_parity_critic_receipts()
    policy = final_production_parity_policy_payload()
    completed_batches = [item for item in batches if item["status"] == "done"]
    local_scans = [item for item in scans if item.get("scan_mode") == "local_repository_file_scan"]
    false_completion_violation_count = sum(
        int(item["violations_found"])
        for item in scans
        if isinstance(item.get("violations_found"), int)
    )
    return {
        "summary": {
            "operator_status": "final_production_parity_gate_visible",
            "source_receipt_count": len(sources),
            "competitor_count": len({item["system"] for item in sources}),
            "current_source_date": "2026-06-11",
            "completed_da_dg_batch_count": len(completed_batches),
            "dh_batch_status": next(item["status"] for item in batches if item["batch"] == "DH"),
            "soak_receipt_count": len(soak),
            "soak_receipts_are_reconciliation_only": all(
                item.get("evidence_mode") == "representative_cross_surface_reconciliation"
                and item.get("actual_runtime_soak_performed") is False
                for item in soak
            ),
            "claim_lift_receipt_count": len(claim_lift),
            "false_completion_scan_count": len(scans),
            "false_completion_violation_count": false_completion_violation_count,
            "all_local_false_completion_scans_clean": all(item.get("violations_found") == 0 for item in local_scans),
            "critic_disposition_count": len(critic),
            "all_sources_have_urls_and_dates": all(item.get("url") and item.get("checked_on") for item in sources),
            "all_sources_are_manual_review_receipts": all(
                item.get("runtime_fetch_performed") is False
                and item.get("source_refresh_kind") == "manual_current_source_review_receipt"
                for item in sources
            ),
            "all_sources_reachable_with_caveats": all(
                item.get("access_status") == "reachable"
                and item.get("access_caveat")
                and item.get("competitor_claim_uncertainty")
                for item in sources
            ),
            "all_completed_da_dg_batches_done_merged_passed": all(
                item["project_status"] == "Done"
                and item["project_pr"] == "Merged"
                and item["code_review"] == "Passed"
                for item in completed_batches
            ),
            "dg_merged_pr": next(item for item in batches if item["batch"] == "DG")["merged_pr"],
            "stale_roadmap_pr_closed": any(item.get("stale_pr_number") == 548 for item in scans),
            "bounded_final_production_parity_wording_allowed": True,
            "bounded_final_production_parity_allowed_wording": FINAL_PRODUCTION_PARITY_ALLOWED_WORDING,
            "full_parity_claim_allowed": False,
            "reference_systems_exceeded_claim_allowed": False,
            "production_ready_claim_allowed": False,
            "secure_private_by_default_claim_allowed": False,
            "claim_boundary": FINAL_PRODUCTION_PARITY_CLAIM_BOUNDARY,
        },
        "reference_system_source_refresh_v3": sources,
        "da_dg_batch_reconciliation_receipts": batches,
        "production_readiness_soak_v1": soak,
        "final_full_parity_claim_lift_v1": claim_lift,
        "false_completion_scan_v3": scans,
        "critic_disposition_receipts": critic,
        "policy": policy,
    }


async def _run_final_production_parity_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_READINESS_SOAK_V1_SUITE_NAME,
        FINAL_FULL_PARITY_CLAIM_LIFT_V1_SUITE_NAME,
        REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SUITE_NAME,
        FALSE_COMPLETION_SCAN_V3_SUITE_NAME,
        BOARD_PR_ISSUE_RECONCILIATION_V3_SUITE_NAME,
    ])


async def build_final_production_parity_report() -> dict[str, Any]:
    summary = await _run_final_production_parity_suites()
    contract = build_final_production_parity_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "final_production_parity_ci_gated_operator_visible"
                if healthy
                else "final_production_parity_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_READINESS_SOAK_V1_SCENARIO_NAMES)
                + len(FINAL_FULL_PARITY_CLAIM_LIFT_V1_SCENARIO_NAMES)
                + len(REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SCENARIO_NAMES)
                + len(FALSE_COMPLETION_SCAN_V3_SCENARIO_NAMES)
                + len(BOARD_PR_ISSUE_RECONCILIATION_V3_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            PRODUCTION_READINESS_SOAK_V1_SUITE_NAME: list(PRODUCTION_READINESS_SOAK_V1_SCENARIO_NAMES),
            FINAL_FULL_PARITY_CLAIM_LIFT_V1_SUITE_NAME: list(FINAL_FULL_PARITY_CLAIM_LIFT_V1_SCENARIO_NAMES),
            REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SUITE_NAME: list(REFERENCE_SYSTEM_SOURCE_REFRESH_V3_SCENARIO_NAMES),
            FALSE_COMPLETION_SCAN_V3_SUITE_NAME: list(FALSE_COMPLETION_SCAN_V3_SCENARIO_NAMES),
            BOARD_PR_ISSUE_RECONCILIATION_V3_SUITE_NAME: list(BOARD_PR_ISSUE_RECONCILIATION_V3_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="final_production_parity"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
