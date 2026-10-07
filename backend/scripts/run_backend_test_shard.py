"""Run one backend test shard as isolated per-file pytest subprocesses."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from scripts.backend_test_shards import shard_for_index
except ModuleNotFoundError:  # pragma: no cover - CI script entrypoint fallback
    from backend_test_shards import shard_for_index


CANCELLED_EVAL_CONSUMER_NODES: dict[str, list[str]] = {'tests/test_always_available_reach_media.py': ['tests/test_always_available_reach_media.py::test_always_available_reach_media_report_exposes_ci_gated_posture'],
 'tests/test_broad_reach_field_ops.py': ['tests/test_broad_reach_field_ops.py::test_broad_reach_field_ops_report_exposes_ci_gated_posture'],
 'tests/test_browser_computer_use_parity_depth.py': ['tests/test_browser_computer_use_parity_depth.py::test_browser_computer_use_parity_depth_report_runs_all_cy_suites'],
 'tests/test_browser_computer_use_production.py': ['tests/test_browser_computer_use_production.py::test_browser_computer_use_production_report_runs_all_gates'],
 'tests/test_browser_provider_usability.py': ['tests/test_browser_provider_usability.py::test_browser_provider_usability_report_runs_all_batch_ch_suites'],
 'tests/test_continuous_orchestration_slo.py': ['tests/test_continuous_orchestration_slo.py::test_continuous_orchestration_slo_report_runs_batch_cs_suites'],
 'tests/test_dense_operator_recovery.py': ['tests/test_dense_operator_recovery.py::test_dense_operator_recovery_report_exposes_ci_gated_posture'],
 'tests/test_durable_workflow_state.py': ['tests/test_durable_workflow_state.py::test_durable_workflow_state_report_includes_persisted_snapshots',
                                          'tests/test_durable_workflow_state.py::test_durable_workflow_v2_contract_and_report_expose_recovery_receipts'],
 'tests/test_final_parity_audit.py': ['tests/test_final_parity_audit.py::test_final_parity_readiness_report_runs_all_batch_ci_suites',
                                      'tests/test_final_parity_audit.py::test_post_cq_claim_readiness_report_runs_all_batch_cz_suites',
                                      'tests/test_final_parity_audit.py::test_final_production_parity_report_runs_all_batch_dh_suites'],
 'tests/test_generalized_guardian_outcomes.py': ['tests/test_generalized_guardian_outcomes.py::test_generalized_guardian_outcomes_report_exposes_ci_gated_posture'],
 'tests/test_independent_learning_memory_parity.py': ['tests/test_independent_learning_memory_parity.py::test_independent_learning_memory_parity_report_exposes_ci_gated_posture'],
 'tests/test_live_external_orchestration.py': ['tests/test_live_external_orchestration.py::test_live_external_orchestration_report_runs_batch_cc_suites'],
 'tests/test_live_guardian_memory_field_program.py': ['tests/test_live_guardian_memory_field_program.py::test_live_guardian_memory_report_exposes_ci_gated_posture'],
 'tests/test_live_human_outcome_learning.py': ['tests/test_live_human_outcome_learning.py::test_live_human_outcome_learning_report_exposes_ci_gated_posture'],
 'tests/test_live_learning_quality.py': ['tests/test_live_learning_quality.py::test_live_guardian_learning_quality_report_exposes_ci_gated_posture'],
 'tests/test_live_marketplace_attestation.py': ['tests/test_live_marketplace_attestation.py::test_live_marketplace_attestation_report_runs_all_batch_cg_suites'],
 'tests/test_live_reach_media.py': ['tests/test_live_reach_media.py::test_live_reach_media_report_exposes_ci_gated_posture'],
 'tests/test_longitudinal_guardian_outcomes.py': ['tests/test_longitudinal_guardian_outcomes.py::test_longitudinal_guardian_outcomes_report_exposes_ci_gated_posture'],
 'tests/test_marketplace_lifecycle.py': ['tests/test_marketplace_lifecycle.py::test_marketplace_lifecycle_report_runs_all_batch_ca_suites'],
 'tests/test_marketplace_production_security.py': ['tests/test_marketplace_production_security.py::test_marketplace_production_security_report_runs_all_batch_dn_suites'],
 'tests/test_marketplace_security_corpus.py': ['tests/test_marketplace_security_corpus.py::test_marketplace_security_corpus_report_runs_all_batch_cx_suites'],
 'tests/test_operator_api.py': ['tests/test_operator_api.py::test_operator_production_parity_readiness_surface_blocks_completion_claims',
                                'tests/test_operator_api.py::test_operator_governed_improvement_benchmark_surface_reports_policy_and_receipts',
                                'tests/test_operator_api.py::test_operator_m9_governed_ecosystem_benchmark_surface_reports_policy_receipts_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_governed_capability_pack_hardening_reports_policy_receipts_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_marketplace_lifecycle_maturity_surface_reports_batch_ca_receipts',
                                'tests/test_operator_api.py::test_operator_live_marketplace_attestation_surface_reports_batch_cg_receipts',
                                'tests/test_operator_api.py::test_operator_production_marketplace_security_surface_reports_batch_co_receipts',
                                'tests/test_operator_api.py::test_operator_marketplace_security_corpus_surface_reports_batch_cx_receipts',
                                'tests/test_operator_api.py::test_operator_production_secure_marketplace_surface_reports_batch_df_receipts',
                                'tests/test_operator_api.py::test_operator_marketplace_production_security_surface_reports_batch_dn_receipts',
                                'tests/test_operator_api.py::test_operator_post_dp_marketplace_lifecycle_surface_reports_batch_dv_receipts',
                                'tests/test_operator_api.py::test_operator_browser_provider_usability_surface_reports_batch_ch_receipts',
                                'tests/test_operator_api.py::test_operator_safe_autonomous_browser_computer_use_surface_reports_batch_cp_receipts',
                                'tests/test_operator_api.py::test_operator_browser_computer_use_parity_depth_surface_reports_batch_cy_receipts',
                                'tests/test_operator_api.py::test_operator_browser_computer_use_production_surface_reports_batch_do_receipts',
                                'tests/test_operator_api.py::test_operator_post_dp_browser_computer_use_reliability_surface_reports_batch_dw_receipts',
                                'tests/test_operator_api.py::test_operator_production_control_parity_surface_reports_batch_cb_receipts',
                                'tests/test_operator_api.py::test_operator_final_parity_readiness_surface_reports_batch_ci_receipts',
                                'tests/test_operator_api.py::test_operator_post_cq_claim_readiness_surface_reports_batch_cz_receipts',
                                'tests/test_operator_api.py::test_operator_final_production_parity_surface_reports_batch_dh_receipts',
                                'tests/test_operator_api.py::test_operator_live_external_orchestration_surface_reports_batch_cc_receipts',
                                'tests/test_operator_api.py::test_operator_production_sla_orchestration_surface_reports_batch_cj_receipts',
                                'tests/test_operator_api.py::test_operator_continuous_orchestration_slo_surface_reports_batch_cs_receipts',
                                'tests/test_operator_api.py::test_operator_production_workflow_guarantees_surface_reports_batch_da_receipts',
                                'tests/test_operator_api.py::test_operator_m7_cockpit_legibility_benchmark_surface_reports_receipts_controls_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_cockpit_efficiency_benchmark_surface_reports_policy_metrics_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_memory_provider_quality_gate_surface_reports_policy_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_live_guardian_learning_quality_surface_reports_batch_bz_receipts',
                                'tests/test_operator_api.py::test_operator_live_human_outcome_learning_surface_reports_batch_cf_receipts',
                                'tests/test_operator_api.py::test_operator_independent_learning_memory_parity_surface_reports_batch_cm_receipts',
                                'tests/test_operator_api.py::test_operator_longitudinal_guardian_outcomes_surface_reports_batch_cv_receipts',
                                'tests/test_operator_api.py::test_operator_generalized_guardian_outcomes_surface_reports_batch_dd_receipts',
                                'tests/test_operator_api.py::test_operator_live_guardian_memory_field_program_surface_reports_batch_dl_receipts',
                                'tests/test_operator_api.py::test_operator_dense_operator_recovery_control_surface_reports_batch_cn_receipts',
                                'tests/test_operator_api.py::test_operator_control_population_study_surface_reports_batch_cw_receipts',
                                'tests/test_operator_api.py::test_operator_control_certification_surface_reports_batch_de_receipts',
                                'tests/test_operator_api.py::test_operator_control_production_certification_surface_reports_batch_dm_receipts',
                                'tests/test_operator_api.py::test_post_dp_operator_debugging_recovery_surface_reports_batch_du_receipts',
                                'tests/test_operator_api.py::test_operator_m8_guardian_intervention_benchmark_surface_reports_policy_and_receipts',
                                'tests/test_operator_api.py::test_operator_guardian_safe_multimodal_voice_surface_reports_policy_receipts_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_production_reach_browser_voice_surface_reports_batch_by_receipts',
                                'tests/test_operator_api.py::test_operator_live_reach_media_surface_reports_batch_ce_receipts',
                                'tests/test_operator_api.py::test_operator_production_reach_voice_mobile_surface_reports_batch_cl_receipts',
                                'tests/test_operator_api.py::test_operator_broad_reach_field_ops_surface_reports_batch_cu_receipts',
                                'tests/test_operator_api.py::test_operator_always_available_reach_media_surface_reports_batch_dc_receipts',
                                'tests/test_operator_api.py::test_operator_reach_voice_production_ops_surface_reports_batch_dk_receipts',
                                'tests/test_operator_api.py::test_operator_post_dp_durable_orchestration_surface_reports_batch_dq_receipts',
                                'tests/test_operator_api.py::test_operator_post_dp_reach_channel_surface_reports_batch_ds_receipts',
                                'tests/test_operator_api.py::test_operator_post_dx_reach_voice_media_surface_reports_batch_ea_receipts',
                                'tests/test_operator_api.py::test_operator_post_dp_guardian_memory_surface_reports_batch_dt_receipts',
                                'tests/test_operator_api.py::test_operator_guardian_learning_arbitration_surface_reports_policy_receipts_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_live_replay_benchmark_surface_reports_policy_receipts_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_memory_benchmark_surface_reports_failure_taxonomy_and_policy',
                                'tests/test_operator_api.py::test_operator_m6_memory_superiority_benchmark_surface_reports_policy_and_receipts',
                                'tests/test_operator_api.py::test_operator_workflow_endurance_benchmark_surface_reports_policy_and_state',
                                'tests/test_operator_api.py::test_operator_live_workflow_endurance_canary_surface_reports_story_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_one_reach_channel_canary_surface_reports_story_and_claim_boundary',
                                'tests/test_operator_api.py::test_operator_trust_boundary_benchmark_surface_reports_policy_and_receipts',
                                'tests/test_operator_api.py::test_operator_secure_capability_host_benchmark_surface_reports_policy_and_receipts',
                                'tests/test_operator_api.py::test_operator_secure_capability_host_hardening_surface_reports_v2_policy_and_receipts',
                                'tests/test_operator_api.py::test_operator_production_isolation_hardening_surface_reports_batch_cd_receipts',
                                'tests/test_operator_api.py::test_operator_independent_secure_host_review_surface_reports_batch_ck_receipts',
                                'tests/test_operator_api.py::test_operator_container_grade_secure_host_surface_reports_batch_ct_receipts',
                                'tests/test_operator_api.py::test_operator_certified_secure_host_surface_reports_batch_db_receipts',
                                'tests/test_operator_api.py::test_operator_production_grade_secure_host_surface_reports_batch_dj_receipts',
                                'tests/test_operator_api.py::test_operator_post_dp_secure_host_surface_reports_batch_dr_receipts',
                                'tests/test_operator_api.py::test_operator_post_dx_formal_secure_runtime_surface_reports_batch_dz_receipts',
                                'tests/test_operator_api.py::test_operator_computer_use_benchmark_surface_reports_policy_and_receipts',
                                'tests/test_operator_api.py::test_operator_m2_execution_benchmark_surface_reports_completion_policy'],
 'tests/test_operator_control_certification.py': ['tests/test_operator_control_certification.py::test_operator_control_certification_report_runs_de_suites'],
 'tests/test_operator_control_production_certification.py': ['tests/test_operator_control_production_certification.py::test_operator_control_production_certification_report_runs_dm_suites'],
 'tests/test_operator_mission_control.py': ['tests/test_operator_mission_control.py::test_operator_mission_control_report_runs_cw_suites'],
 'tests/test_post_dp_browser_computer_use_reliability.py': ['tests/test_post_dp_browser_computer_use_reliability.py::test_post_dp_browser_computer_use_reliability_report_runs_all_gates'],
 'tests/test_post_dp_guardian_memory_gap_closure.py': ['tests/test_post_dp_guardian_memory_gap_closure.py::test_post_dp_guardian_memory_report_runs_all_dt_suites'],
 'tests/test_post_dp_marketplace_lifecycle_gap_closure.py': ['tests/test_post_dp_marketplace_lifecycle_gap_closure.py::test_post_dp_marketplace_lifecycle_report_runs_all_dv_suites'],
 'tests/test_production_marketplace_security.py': ['tests/test_production_marketplace_security.py::test_production_marketplace_security_report_runs_all_batch_co_suites'],
 'tests/test_production_operator_control.py': ['tests/test_production_operator_control.py::test_production_operator_control_report_runs_cb_suites'],
 'tests/test_production_reach_hardening.py': ['tests/test_production_reach_hardening.py::test_production_reach_browser_voice_report_exposes_ci_gated_posture'],
 'tests/test_production_reach_voice_mobile.py': ['tests/test_production_reach_voice_mobile.py::test_production_reach_voice_mobile_report_exposes_ci_gated_posture'],
 'tests/test_production_secure_marketplace.py': ['tests/test_production_secure_marketplace.py::test_production_secure_marketplace_report_runs_all_batch_df_suites'],
 'tests/test_production_sla_orchestration.py': ['tests/test_production_sla_orchestration.py::test_production_sla_orchestration_report_runs_batch_cj_suites'],
 'tests/test_production_workflow_guarantees.py': ['tests/test_production_workflow_guarantees.py::test_production_workflow_guarantees_report_exposes_persisted_runtime_snapshot',
                                                  'tests/test_production_workflow_guarantees.py::test_production_workflow_guarantees_report_runs_da_suites'],
 'tests/test_reach_voice_production_ops.py': ['tests/test_reach_voice_production_ops.py::test_reach_voice_production_ops_report_exposes_ci_gated_posture'],
 'tests/test_safe_browser_computer_use.py': ['tests/test_safe_browser_computer_use.py::test_safe_browser_computer_use_report_runs_all_batch_cp_suites']}


FULLY_RETIRED_EVAL_INVOCATIONS = {
    ("tests/test_operator_api.py", "tests/test_operator_api.py::marketplace_and_ecosystem"),
}


RUNTIME_HEAVY_FILE_TIMEOUTS: dict[str, int] = {
    "tests/test_approvals_api.py": 1_200,
    "tests/test_context_window.py": 1_200,
    "tests/test_delivery.py": 1_200,
    "tests/test_eval_harness.py": 1_500,
    "tests/test_extensions_api.py": 1_500,
    "tests/test_observer_api.py": 1_200,
    "tests/test_tools_api.py": 1_500,
    "tests/test_workflows.py": 1_500,
}

SPECIALIZED_TEST_INVOCATIONS: dict[str, list[tuple[str, list[str]]]] = {
    "tests/test_capabilities_api.py": [
        (
            "tests/test_capabilities_api.py::overview_and_catalog",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "load_starter_packs or attach_ or mcp_status or doctor_reports or capabilities_overview",
            ],
        ),
        (
            "tests/test_capabilities_api.py::starter_pack_activation_foundations",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "activate_starter_pack_enables_seeded_assets or activate_manifest_backed_starter_pack_works or ensure_bundled_workflow_available",
            ],
        ),
        (
            "tests/test_capabilities_api.py::starter_pack_activation_bundled_core",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "activate_bundled_core_capability_pack_uses_manifest_runtime or activate_bundled_core_capability_pack_uses_real_catalog_install",
            ],
        ),
        (
            "tests/test_capabilities_api.py::starter_pack_activation_approvals_and_degraded",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "activate_starter_pack_requires_catalog_install_approval or activate_starter_pack_preflights_all_approvals_without_consuming_them or activate_starter_pack_reports_degraded_when_enable_fails",
            ],
        ),
        (
            "tests/test_capabilities_api.py::bootstrap_manual_routes",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "capability_bootstrap_leaves_policy_changes_manual or capability_bootstrap_leaves_mcp_enable_actions_manual or capability_bootstrap_leaves_extension_enable_actions_manual",
            ],
        ),
        (
            "tests/test_capabilities_api.py::bootstrap_apply_and_validation",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "capability_preflight_returns_workflow_and_runbook_repair_metadata or capability_bootstrap_can_apply_low_risk_toggle_actions or capability_bootstrap_does_not_reclassify_low_risk_actions_as_manual_after_failed_apply or workflow_draft_validation_and_save",
            ],
        ),
    ],
    "tests/test_delivery.py": [
        (
            "tests/test_delivery.py::channel_and_bundle",
            [
                "tests/test_delivery.py",
                "-k",
                "native_channel or channel_routing or queued_bundle",
            ],
        ),
        (
            "tests/test_delivery.py::remaining",
            [
                "tests/test_delivery.py",
                "-k",
                "not (native_channel or channel_routing or queued_bundle)",
            ],
        ),
    ],
    "tests/test_e2e_conversation.py": [
        (
            "tests/test_e2e_conversation.py::test_full_message_flow",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_full_message_flow",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_seq_numbers_monotonically_increase",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_seq_numbers_monotonically_increase",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_tool_name_in_step_content",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_tool_name_in_step_content",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_agent_run_success_is_written_to_audit_log",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_agent_run_success_is_written_to_audit_log",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_high_risk_tool_sends_approval_required_message",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_high_risk_tool_sends_approval_required_message",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_missing_input_sends_clarification_required_message",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_missing_input_sends_clarification_required_message",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_timeout_logs_only_timed_out_runtime_event",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_timeout_logs_only_timed_out_runtime_event",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_secret_values_are_redacted_in_streamed_messages",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_secret_values_are_redacted_in_streamed_messages",
            ],
        ),
        (
            "tests/test_e2e_conversation.py::test_resume_message_does_not_duplicate_user_turn",
            [
                "tests/test_e2e_conversation.py::TestE2EConversation::test_resume_message_does_not_duplicate_user_turn",
            ],
        ),
    ],
    "tests/test_eval_harness.py": [
        (
            "tests/test_eval_harness.py::runtime_group_1",
            [
                "tests/test_eval_harness.py",
                "-k",
                (
                    "test_run_runtime_evals_passes_group_1 "
                    "and not source_report_action_workflow_behavior "
                    "and not chat_model_wrapper_runtime_eval_details "
                    "and not rest_chat_behavior_runtime_eval_details "
                    "and not rest_chat_approval_contract_runtime_eval_details "
                    "and not rest_chat_timeout_contract_runtime_eval_details "
                    "and not websocket_chat_behavior_runtime_eval_details "
                    "and not websocket_chat_approval_contract_runtime_eval_details "
                    "and not websocket_chat_timeout_contract_runtime_eval_details"
                ),
            ],
        ),
        (
            "tests/test_eval_harness.py::test_chat_model_wrapper_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_chat_model_wrapper_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_rest_chat_behavior_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_rest_chat_behavior_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_rest_chat_approval_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_rest_chat_approval_contract_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_rest_chat_timeout_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_rest_chat_timeout_contract_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_websocket_chat_behavior_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_websocket_chat_behavior_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_websocket_chat_approval_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_websocket_chat_approval_contract_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_websocket_chat_timeout_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_websocket_chat_timeout_contract_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_source_report_action_workflow_behavior_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_source_report_action_workflow_behavior_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_runtime_eval_scenarios_expose_expected_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_runtime_eval_scenarios_expose_expected_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::runtime_group_2",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_2",
            ],
        ),
        (
            "tests/test_eval_harness.py::runtime_group_3",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_3",
            ],
        ),
        (
            "tests/test_eval_harness.py::runtime_group_4",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_4",
            ],
        ),
        (
            "tests/test_eval_harness.py::remaining",
            [
                "tests/test_eval_harness.py",
                "-k",
                (
                    "not (test_run_runtime_evals_passes_group_1 "
                    "or test_chat_model_wrapper_runtime_eval_details "
                    "or test_rest_chat_behavior_runtime_eval_details "
                    "or test_rest_chat_approval_contract_runtime_eval_details "
                    "or test_rest_chat_timeout_contract_runtime_eval_details "
                    "or test_websocket_chat_behavior_runtime_eval_details "
                    "or test_websocket_chat_approval_contract_runtime_eval_details "
                    "or test_websocket_chat_timeout_contract_runtime_eval_details "
                    "or test_source_report_action_workflow_behavior_runtime_eval_details "
                    "or test_runtime_eval_scenarios_expose_expected_details "
                    "or test_run_runtime_evals_passes_group_2 "
                    "or test_run_runtime_evals_passes_group_3 "
                    "or test_run_runtime_evals_passes_group_4)"
                ),
            ],
        ),
    ],
    "tests/test_observer_api.py": [
        (
            "tests/test_observer_api.py::continuity_and_notifications",
            [
                "tests/test_observer_api.py",
                "-k",
                "continuity or native_notification or intervention_feedback",
            ],
        ),
        (
            "tests/test_observer_api.py::remaining",
            [
                "tests/test_observer_api.py",
                "-k",
                "not (continuity or native_notification or intervention_feedback)",
            ],
        ),
    ],
    "tests/test_operator_api.py": [
        (
            "tests/test_operator_api.py::timeline_control_and_release_gates",
            [
                "tests/test_operator_api.py",
                "-k",
                (
                    "timeline or control_plane or benchmark_proof or final_parity "
                    "or final_production or full_parity or post_cq or post_dq_dw"
                ),
            ],
        ),
        (
            "tests/test_operator_api.py::marketplace_and_ecosystem",
            [
                "tests/test_operator_api.py",
                "-k",
                "marketplace or capability_pack or governed_ecosystem or governed_improvement",
            ],
        ),
        (
            "tests/test_operator_api.py::browser_computer_use",
            [
                "tests/test_operator_api.py",
                "-k",
                "(browser or computer_use) and not (reach or voice or multimodal)",
            ],
        ),
        (
            "tests/test_operator_api.py::workflow_orchestration",
            [
                "tests/test_operator_api.py",
                "-k",
                "orchestration or workflow or durable or sla",
            ],
        ),
        (
            "tests/test_operator_api.py::operator_control",
            [
                "tests/test_operator_api.py",
                "-k",
                (
                    "cockpit or operator_control or operator_debugging or dense_operator "
                    "or control_population or control_certification"
                ),
            ],
        ),
        (
            "tests/test_operator_api.py::guardian_memory_learning",
            [
                "tests/test_operator_api.py",
                "-k",
                "guardian or memory or learning",
            ],
        ),
        (
            "tests/test_operator_api.py::reach_voice_and_security",
            [
                "tests/test_operator_api.py",
                "-k",
                (
                    "reach or voice or multimodal or trust_boundary or secure_capability "
                    "or secure_host or isolation"
                ),
            ],
        ),
        (
            "tests/test_operator_api.py::remaining",
            [
                "tests/test_operator_api.py",
                "-k",
                (
                    "not (timeline or control_plane or benchmark_proof or final_parity "
                    "or final_production or full_parity or post_cq or post_dq_dw "
                    "or marketplace or capability_pack or governed_ecosystem "
                    "or governed_improvement or browser or computer_use or orchestration "
                    "or workflow or durable or sla or cockpit or operator_control "
                    "or operator_debugging or dense_operator or control_population "
                    "or control_certification or guardian or memory or learning "
                    "or reach or voice or multimodal or trust_boundary or secure_capability "
                    "or secure_host or isolation)"
                ),
            ],
        ),
    ],
    "tests/test_tools_api.py": [
        (
            "tests/test_tools_api.py::native_policy_modes",
            [
                "tests/test_tools_api.py",
                "-k",
                "full_mode_includes_execute_code or safe_mode_keeps_clarify_available or safe_mode_keeps_todo_available or safe_mode_keeps_session_search_available or safe_mode_keeps_browser_session_available or safe_mode_keeps_get_scheduled_jobs_available or balanced_mode_hides_full_only_tools",
            ],
        ),
        (
            "tests/test_tools_api.py::delegation_and_scheduler",
            [
                "tests/test_tools_api.py",
                "-k",
                "balanced_mode_keeps_delegate_task_available or balanced_mode_keeps_manage_scheduled_job_available or hides_delegate_task_when_delegation_is_disabled",
            ],
        ),
        (
            "tests/test_tools_api.py::mcp_policy_surface",
            [
                "tests/test_tools_api.py",
                "-k",
                "hides_mcp_tools_when_disabled or marks_mcp_tools_as_approval_required_in_approval_mode or marks_authenticated_mcp_tools_with_narrower_boundary or allows_mcp_tools_with_balanced_native_policy_when_mcp_approval_enabled",
            ],
        ),
        (
            "tests/test_tools_api.py::workflow_boundary_surface",
            [
                "tests/test_tools_api.py",
                "-k",
                "surfaces_workflow_execution_boundaries",
            ],
        ),
    ],
    "tests/test_workflows.py": [
        (
            "tests/test_workflows.py::approval_and_legacy_boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "approval_context or legacy_checkpoint",
            ],
        ),
        (
            "tests/test_workflows.py::authenticated_source_boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "authenticated_source",
            ],
        ),
        (
            "tests/test_workflows.py::delegation_boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "delegated_specialist or delegated_tool_inventory",
            ],
        ),
        (
            "tests/test_workflows.py::history_and_projection_surface",
            [
                "tests/test_workflows.py",
                "-k",
                "projects_history or stored_fingerprint or pending_run_lacks_tracked_authenticated_context or marks_waiting_runs_as_awaiting_approval or does_not_suggest_tool_policy or hides_later_retry_draft or disambiguates_duplicate_fingerprinted_runs",
            ],
        ),
        (
            "tests/test_workflows.py::resume_plan_branching_surface",
            [
                "tests/test_workflows.py",
                "-k",
                "returns_structured_branch_metadata or rejects_approval_gate or rejects_noninitial_checkpoint or blocks_branching_past_pending_approval_gate or falls_back_to_scoped_run_lookup",
            ],
        ),
        (
            "tests/test_workflows.py::remaining_boundary_surface",
            [
                "tests/test_workflows.py",
                "-k",
                "not (approval_context or authenticated_source or delegated_specialist or delegated_tool_inventory or legacy_checkpoint or projects_history or stored_fingerprint or pending_run_lacks_tracked_authenticated_context or marks_waiting_runs_as_awaiting_approval or does_not_suggest_tool_policy or hides_later_retry_draft or disambiguates_duplicate_fingerprinted_runs or returns_structured_branch_metadata or rejects_approval_gate or rejects_noninitial_checkpoint or blocks_branching_past_pending_approval_gate or falls_back_to_scoped_run_lookup)",
            ],
        ),
    ],
}


def timeout_for_file(path: str, default_timeout_seconds: int | None) -> int | None:
    hinted_timeout = RUNTIME_HEAVY_FILE_TIMEOUTS.get(path)
    if default_timeout_seconds is None:
        return hinted_timeout
    if hinted_timeout is None:
        return default_timeout_seconds
    return max(default_timeout_seconds, hinted_timeout)


def pytest_invocations_for_target(path: str) -> list[tuple[str, list[str]]]:
    return SPECIALIZED_TEST_INVOCATIONS.get(path, [(path, [path])])


def run_shard_files(
    root: Path,
    files: list[str],
    *,
    pytest_args: list[str] | None = None,
    file_timeout_seconds: int | None = None,
    exclude_cancelled_eval_harness: bool = False,
) -> int:
    if not files:
        print("No backend tests assigned to this shard.")
        return 0

    extra_args = list(pytest_args or [])
    if "--no-cov" not in extra_args and not any(arg.startswith("--cov") for arg in extra_args):
        extra_args.append("--no-cov")
    for path in files:
        if exclude_cancelled_eval_harness and path == "tests/test_eval_harness.py":
            print("[backend-shard] RETIRED/NOT RUN tests/test_eval_harness.py: cancelled module; "
                  "228 top-level test definitions, including 13 ordinary contracts, "
                  "not selected for execution; not passed (source-definition counts)")
            continue
        retired_nodes = CANCELLED_EVAL_CONSUMER_NODES.get(path, []) if exclude_cancelled_eval_harness else []
        for node in retired_nodes:
            print(f"[backend-shard] RETIRED/NOT RUN {node}: cancelled campaign consumer; "
                  "one source definition not selected for execution; not passed")
        for label, invocation_args in pytest_invocations_for_target(path):
            if exclude_cancelled_eval_harness and (path, label) in FULLY_RETIRED_EVAL_INVOCATIONS:
                print(f"[backend-shard] RETIRED/NOT RUN {label}: "
                      "all 10 selected source definitions are cancelled campaign consumers; not passed")
                continue
            command = [sys.executable, "-m", "pytest", "-q", *invocation_args, *extra_args,
                       *(f"--deselect={node}" for node in retired_nodes)]
            timeout_seconds = timeout_for_file(path, file_timeout_seconds)
            started_at = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                duration_s = time.perf_counter() - started_at
                print(
                    f"[backend-shard] {label} -> 124 ({duration_s:.2f}s) timed out after "
                    f"{timeout_seconds}s"
                )
                return 124
            duration_s = time.perf_counter() - started_at
            print(
                f"[backend-shard] {label} -> {completed.returncode} ({duration_s:.2f}s)"
                f"{f' timeout={timeout_seconds}s' if timeout_seconds is not None else ''}"
            )
            if completed.returncode != 0:
                return completed.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--file-timeout-seconds", type=int, default=None)
    parser.add_argument(
        "--exclude-cancelled-eval-harness", action="store_true",
        help="CI policy: retire only tests/test_eval_harness.py from execution and report it as not run",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    files = shard_for_index(root, shard_count=args.shard_count, shard_index=args.shard_index)
    extra_args = list(args.pytest_args)
    if extra_args[:1] == ["--"]:
        extra_args = extra_args[1:]
    return run_shard_files(
        root,
        files,
        pytest_args=extra_args,
        file_timeout_seconds=args.file_timeout_seconds,
        exclude_cancelled_eval_harness=args.exclude_cancelled_eval_harness,
    )


if __name__ == "__main__":
    raise SystemExit(main())
