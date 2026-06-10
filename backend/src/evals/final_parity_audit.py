"""Batch CI final source-backed parity and exceedance audit receipts.

This module reconciles current-source competitor evidence, production-train
batch state, claim-ledger boundaries, and operator-visible proof surfaces. It
is a final audit gate, not blanket full-parity, superiority, or production-ready
evidence.
"""

from __future__ import annotations

from typing import Any


FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME = "final_source_backed_parity_audit"
FINAL_SOURCE_BACKED_PARITY_AUDIT_SCENARIO_NAMES = (
    "final_current_source_coverage_behavior",
    "final_competitor_pressure_mapping_behavior",
    "final_batch_completion_evidence_behavior",
    "final_residual_gap_boundary_behavior",
    "final_source_date_freshness_behavior",
)
FINAL_CLAIM_LEDGER_RECONCILIATION_SUITE_NAME = "final_claim_ledger_reconciliation"
FINAL_CLAIM_LEDGER_RECONCILIATION_SCENARIO_NAMES = (
    "final_forbidden_claim_block_behavior",
    "final_allowed_wording_scope_behavior",
    "final_claim_ledger_issue_link_behavior",
    "final_claim_boundary_operator_surface_behavior",
    "final_critic_disposition_required_behavior",
)
OPERATOR_FINAL_PARITY_READINESS_REPORT_SUITE_NAME = "operator_final_parity_readiness_report"
OPERATOR_FINAL_PARITY_READINESS_REPORT_SCENARIO_NAMES = (
    "operator_final_readiness_report_surface_behavior",
    "operator_final_board_reconciliation_behavior",
    "operator_final_benchmark_aggregate_behavior",
    "operator_final_residual_risk_visibility_behavior",
    "operator_final_no_false_completion_behavior",
)
FINAL_PARITY_AUDIT_CLAIM_BOUNDARY = (
    "final_source_backed_audit_not_full_parity_superiority_or_production_ready_claim"
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
    return [
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
            "residual_gap": "Batch CH adds provider/usability receipts but not safe autonomous browser use or full browser parity.",
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
    receipts.append({
        "batch": "CI",
        "issue": 497,
        "primary_suite": FINAL_SOURCE_BACKED_PARITY_AUDIT_SUITE_NAME,
        "merged_pr": None,
        "status": "self_referential_final_audit_batch",
        "project_fields_required": ["Queue", "Lane", "Priority", "Size", "Status", "Code Review", "PR"],
        "project_status": "owned_by_github_project_until_pr_merge",
        "project_pr": "owned_by_linked_pull_request_until_pr_merge",
        "code_review": "owned_by_linked_pull_request_until_pr_merge",
        "project_truth_source": "GitHub issue #497 and its Project item are authoritative for live PR/review fields",
        "operator_visible": True,
    })
    receipts.append({
        "batch": "CO",
        "issue": 510,
        "primary_suite": "independent_package_security_review",
        "merged_pr": None,
        "status": "active_branch_receipts_visible_until_pr_merge",
        "project_fields_required": ["Queue", "Lane", "Priority", "Size", "Status", "Code Review", "PR"],
        "project_status": "owned_by_github_project_until_pr_merge",
        "project_pr": "owned_by_linked_pull_request_until_pr_merge",
        "code_review": "owned_by_linked_pull_request_until_pr_merge",
        "project_truth_source": "GitHub issue #510 and its Project item are authoritative for live PR/review fields",
        "operator_visible": True,
    })
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
            "gap": "safe autonomous browser/computer-use and full browser parity remain unproven",
            "blocking_claims": ["safe_browser_automation", "full_browser_parity"],
            "required_stronger_evidence": "broad live task depth, credential partitioning, site-specific recovery, and independent usability evidence",
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
            "final_batch_status": next(item["status"] for item in batches if item["batch"] == "CI"),
            "claim_ledger_receipt_count": len(claims),
            "residual_gap_count": len(gaps),
            "blocked_claim_count": len(policy["blocked_claims"]),
            "critic_disposition_count": len(critic),
            "all_sources_have_urls_and_dates": all(item.get("url") and item.get("checked_on") for item in sources),
            "all_completed_batches_done_merged_passed": all(
                item["project_status"] == "Done"
                and item["project_pr"] == "Merged"
                and item["code_review"] == "Passed"
                for item in completed_batches
            ),
            "full_parity_claim_allowed": False,
            "reference_systems_exceeded_claim_allowed": False,
            "claim_boundary": FINAL_PARITY_AUDIT_CLAIM_BOUNDARY,
        },
        "current_source_receipts": sources,
        "batch_reconciliation_receipts": batches,
        "claim_ledger_reconciliation": claims,
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
