"""Tests for the repeatable runtime evaluation harness."""

import asyncio
import json
from unittest.mock import patch

import pytest

from config.settings import settings
from src.evals import harness
from src.evals.harness import available_benchmark_suites, available_scenarios, main, run_benchmark_suites, run_runtime_evals


def _runtime_eval_scenario_names() -> list[str]:
    return [scenario.name for scenario in available_scenarios()]


def _runtime_eval_groups() -> list[tuple[str, list[str]]]:
    scenario_names = _runtime_eval_scenario_names()
    group_size = 26
    return [
        (f"group_{index + 1}", scenario_names[index : index + group_size])
        for index in range(0, len(scenario_names), group_size)
    ]


def _runtime_group_1_without_source_report() -> list[str]:
    _, scenario_names = _runtime_eval_groups()[0]
    return [
        scenario_name
        for scenario_name in scenario_names
        if scenario_name != "source_report_action_workflow_behavior"
    ]


def test_run_runtime_evals_passes_group_1():
    scenario_names = _runtime_group_1_without_source_report()
    summary = asyncio.run(run_runtime_evals(scenario_names))

    result_names = {result.name for result in summary.results}

    assert summary.total == len(scenario_names)
    assert summary.failed == 0
    assert result_names == set(scenario_names)


def test_run_runtime_evals_passes_group_2():
    _, scenario_names = _runtime_eval_groups()[1]
    summary = asyncio.run(run_runtime_evals(scenario_names))

    result_names = {result.name for result in summary.results}

    assert summary.total == len(scenario_names)
    assert summary.failed == 0
    assert result_names == set(scenario_names)


def test_run_runtime_evals_passes_group_3():
    _, scenario_names = _runtime_eval_groups()[2]
    summary = asyncio.run(run_runtime_evals(scenario_names))

    result_names = {result.name for result in summary.results}

    assert summary.total == len(scenario_names)
    assert summary.failed == 0
    assert result_names == set(scenario_names)


def test_run_runtime_evals_passes_group_4():
    _, scenario_names = _runtime_eval_groups()[3]
    summary = asyncio.run(run_runtime_evals(scenario_names))

    result_names = {result.name for result in summary.results}

    assert summary.total == len(scenario_names)
    assert summary.failed == 0
    assert result_names == set(scenario_names)


def test_run_runtime_evals_can_filter_specific_scenarios():
    summary = asyncio.run(run_runtime_evals(["agent_local_runtime_profile", "observer_delivery_gate_audit"]))

    assert summary.total == 2
    assert summary.failed == 0
    assert [result.name for result in summary.results] == [
        "agent_local_runtime_profile",
        "observer_delivery_gate_audit",
    ]


def test_run_runtime_evals_rejects_unknown_scenarios():
    with pytest.raises(ValueError, match="Unknown eval scenario"):
        asyncio.run(run_runtime_evals(["missing-scenario"]))


def test_source_report_action_workflow_behavior_runtime_eval_details():
    summary = asyncio.run(run_runtime_evals(["source_report_action_workflow_behavior"]))

    assert summary.total == 1
    assert summary.failed == 0

    details = summary.results[0].details

    assert details["report_status"] == "ready"
    assert details["publish_status"] == "approval_required"
    assert details["publish_action_kind"] == "comment"
    assert details["publish_target_reference"] == "seraph-quest/seraph#343"
    assert details["recommended_runbook"] == "runbook:source-progress-report"
    assert details["recommended_starter_pack"] == "source-progress-report"
    assert details["execution_status"] == "ok"
    assert details["execution_tool_name"] == "add_comment_to_issue"
    assert details["execution_argument_keys"] == [
        "comment",
        "issue_number",
        "repo_full_name",
    ]
    assert details["review_publish_status"] == "approval_required"
    assert details["review_publish_contract"] == "code_activity.write"
    assert details["review_publish_action_kind"] == "review"
    assert details["review_fixed_argument_keys"] == ["action", "file_comments"]
    assert details["review_execution_status"] == "ok"
    assert details["review_execution_tool_name"] == "add_review_to_pr"
    assert details["review_execution_argument_keys"] == [
        "action",
        "file_comments",
        "pr_number",
        "repo_full_name",
        "review",
    ]


def test_governed_self_evolution_behavior_runtime_eval_details():
    summary = asyncio.run(run_runtime_evals(["governed_self_evolution_behavior"]))

    assert summary.total == 1
    assert summary.failed == 0

    details = summary.results[0].details

    assert "prompt_pack" in details["target_types"]
    assert "skill" in details["target_types"]
    assert details["proposal_status"] == "saved"
    assert details["proposal_quality_state"] in {"guarded", "ready"}
    assert details["saved_candidate_has_goal_section"] is True
    assert details["saved_candidate_path"].endswith("extensions/workspace-capabilities/skills/web-briefing-review-candidate.md")
    assert details["stored_receipt_candidate_name"] == "web-briefing Review Candidate"
    assert details["stored_receipt_change_summary_count"] >= 1
    assert details["stored_receipt_review_risk_count"] >= 1
    assert details["proposal_change_summary_present"] is True
    assert details["proposal_review_risks_present"] is True
    assert details["blocked_status"] is True
    assert details["blocked_constraint"] == "blocked"
    assert details["blocked_tokens"] == ["vault://"]
    assert details["blocked_review_risk_mentions_trace_coverage"] is True


def test_benchmark_proof_surface_behavior_runtime_eval_details():
    summary = asyncio.run(run_runtime_evals(["benchmark_proof_surface_behavior"]))

    assert summary.total == 1
    assert summary.failed == 0

    details = summary.results[0].details
    assert details["suite_count"] == 6
    assert details["guardian_memory_suite_present"] is True
    assert details["guardian_user_model_suite_present"] is True
    assert details["memory_suite_present"] is True
    assert details["computer_suite_present"] is True
    assert details["planning_suite_present"] is True
    assert details["governed_suite_present"] is True
    assert details["required_suite_count_matches"] is True
    assert details["gate_requires_review"] is True
    assert details["gate_blocks_constraint_failure"] is True
    assert details["proof_contract"] == "deterministic_benchmark_suites_plus_review_receipts"


def test_run_benchmark_suites_executes_unique_suite_scenarios():
    summary = asyncio.run(run_benchmark_suites(["governed_improvement"]))

    result_names = {result.name for result in summary.results}

    assert summary.failed == 0
    assert result_names == {
        "governed_self_evolution_behavior",
        "capability_repair_behavior",
        "capability_preflight_behavior",
    }


def test_run_benchmark_suites_executes_guardian_memory_quality_suite():
    summary = asyncio.run(run_benchmark_suites(["guardian_memory_quality"]))

    result_names = {result.name for result in summary.results}

    assert summary.failed == 0
    assert result_names == {
        "memory_engineering_retrieval_benchmark_behavior",
        "memory_contradiction_ranking_behavior",
        "memory_selective_forgetting_surface_behavior",
        "operator_memory_benchmark_surface_behavior",
        "memory_provider_user_model_behavior",
        "memory_provider_stale_evidence_behavior",
        "memory_provider_writeback_behavior",
        "memory_reconciliation_policy_behavior",
    }


def test_run_benchmark_suites_executes_guardian_user_model_restraint_suite():
    summary = asyncio.run(run_benchmark_suites(["guardian_user_model_restraint"]))

    result_names = {result.name for result in summary.results}

    assert summary.failed == 0
    assert result_names == {
        "guardian_user_model_continuity_behavior",
        "guardian_clarification_restraint_behavior",
        "guardian_judgment_behavior",
        "operator_guardian_state_surface_behavior",
    }


def test_process_recovery_boundary_behavior_runtime_eval_details():
    summary = asyncio.run(run_runtime_evals(["process_recovery_boundary_behavior"]))

    assert summary.total == 1
    assert summary.failed == 0

    details = summary.results[0].details

    assert details["session_scoped"] is True
    assert details["output_path_within_workspace"] is False
    assert details["owner_list_includes_process"] is True
    assert details["owner_output_visible"] is True
    assert details["owner_stop_succeeds"] is True
    assert details["other_list_hidden"] is True
    assert details["other_read_hidden"] is True
    assert details["other_stop_hidden"] is True


def test_main_lists_available_scenarios(capsys):
    exit_code = main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "chat_model_wrapper" in captured.out
    assert "rest_chat_behavior" in captured.out
    assert "rest_chat_approval_contract" in captured.out
    assert "rest_chat_timeout_contract" in captured.out
    assert "websocket_chat_behavior" in captured.out
    assert "websocket_chat_approval_contract" in captured.out
    assert "websocket_chat_timeout_contract" in captured.out
    assert "strategist_tick_behavior" in captured.out
    assert "strategist_tick_learning_continuity_behavior" in captured.out
    assert "guardian_state_synthesis" in captured.out
    assert "guardian_world_model_behavior" in captured.out
    assert "guardian_judgment_behavior" in captured.out
    assert "guardian_user_model_continuity_behavior" in captured.out
    assert "guardian_clarification_restraint_behavior" in captured.out
    assert "guardian_long_horizon_learning_behavior" in captured.out
    assert "observer_refresh_behavior" in captured.out
    assert "observer_delivery_decision_behavior" in captured.out
    assert "native_presence_notification_behavior" in captured.out
    assert "native_desktop_shell_behavior" in captured.out
    assert "cross_surface_notification_controls_behavior" in captured.out
    assert "cross_surface_continuity_behavior" in captured.out
    assert "intervention_policy_behavior" in captured.out
    assert "salience_calibration_behavior" in captured.out
    assert "observer_delivery_salience_confidence_behavior" in captured.out
    assert "guardian_feedback_loop" in captured.out
    assert "provider_fallback_chain" in captured.out
    assert "provider_health_reroute" in captured.out
    assert "local_runtime_profile" in captured.out
    assert "helper_local_runtime_paths" in captured.out
    assert "context_window_summary_audit" in captured.out
    assert "agent_local_runtime_profile" in captured.out
    assert "delegation_local_runtime_profile" in captured.out
    assert "delegation_secret_boundary_behavior" in captured.out
    assert "delegated_tool_workflow_behavior" in captured.out
    assert "delegated_tool_workflow_degraded_behavior" in captured.out
    assert "workflow_composition_behavior" in captured.out
    assert "workflow_approval_threading_behavior" in captured.out
    assert "threaded_operator_timeline_behavior" in captured.out
    assert "background_session_handoff_behavior" in captured.out
    assert "workflow_context_condenser_behavior" in captured.out
    assert "workflow_operating_layer_behavior" in captured.out
    assert "engineering_memory_bundle_behavior" in captured.out
    assert "operator_continuity_graph_behavior" in captured.out
    assert "operator_guardian_state_surface_behavior" in captured.out
    assert "workflow_boundary_blocked_surface_behavior" in captured.out
    assert "approval_explainability_surface_behavior" in captured.out
    assert "source_report_action_workflow_behavior" in captured.out
    assert "governed_self_evolution_behavior" in captured.out
    assert "benchmark_proof_surface_behavior" in captured.out
    assert "capability_repair_behavior" in captured.out
    assert "capability_preflight_behavior" in captured.out
    assert "activity_ledger_attribution_behavior" in captured.out
    assert "imported_capability_surface_behavior" in captured.out
    assert "mcp_specialist_local_runtime_profile" in captured.out
    assert "embedding_runtime_audit" in captured.out
    assert "vector_store_runtime_audit" in captured.out
    assert "soul_runtime_audit" in captured.out
    assert "filesystem_runtime_audit" in captured.out
    assert "vault_runtime_audit" in captured.out
    assert "runtime_model_overrides" in captured.out
    assert "runtime_fallback_overrides" in captured.out
    assert "runtime_profile_preferences" in captured.out
    assert "runtime_path_patterns" in captured.out
    assert "provider_policy_capabilities" in captured.out
    assert "provider_policy_scoring" in captured.out
    assert "provider_policy_safeguards" in captured.out
    assert "provider_routing_decision_audit" in captured.out
    assert "memory_engineering_retrieval_benchmark_behavior" in captured.out
    assert "memory_contradiction_ranking_behavior" in captured.out
    assert "memory_selective_forgetting_surface_behavior" in captured.out
    assert "operator_memory_benchmark_surface_behavior" in captured.out
    assert "session_bound_llm_trace" in captured.out
    assert "session_consolidation_behavior" in captured.out
    assert "memory_commitment_continuity_behavior" in captured.out
    assert "memory_collaborator_lookup_behavior" in captured.out
    assert "memory_provider_user_model_behavior" in captured.out
    assert "memory_provider_stale_evidence_behavior" in captured.out
    assert "memory_provider_writeback_behavior" in captured.out
    assert "bounded_memory_snapshot_behavior" in captured.out
    assert "memory_supersession_filter_behavior" in captured.out
    assert "memory_decay_contradiction_cleanup_behavior" in captured.out
    assert "memory_reconciliation_policy_behavior" in captured.out
    assert "procedural_memory_adaptation_behavior" in captured.out
    assert "scheduled_local_runtime_profile" in captured.out
    assert "process_recovery_boundary_behavior" in captured.out
    assert "daily_briefing_delivery_behavior" in captured.out
    assert "shell_tool_runtime_audit" in captured.out
    assert "browser_runtime_audit" in captured.out


def test_main_lists_available_benchmark_suites(capsys):
    exit_code = main(["--list-benchmark-suites"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "guardian_memory_quality" in captured.out
    assert "guardian_user_model_restraint" in captured.out
    assert "memory_continuity_workflows" in captured.out
    assert "computer_use_browser_desktop" in captured.out
    assert "planning_retrieval_reporting" in captured.out
    assert "governed_improvement" in captured.out
    assert available_benchmark_suites() == (
        "guardian_memory_quality",
        "guardian_user_model_restraint",
        "memory_continuity_workflows",
        "computer_use_browser_desktop",
        "planning_retrieval_reporting",
        "governed_improvement",
    )


def test_main_emits_json_summary(capsys):
    exit_code = main(["--scenario", "shell_tool_timeout_contract", "--indent", "0"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["failed"] == 0
    assert payload["results"][0]["name"] == "shell_tool_timeout_contract"


def test_make_sync_client_with_db_unwinds_patches_when_client_startup_fails():
    original_workspace_dir = settings.workspace_dir

    with patch("src.evals.harness.TestClient", side_effect=RuntimeError("startup failed")):
        with pytest.raises(RuntimeError, match="startup failed"):
            harness._make_sync_client_with_db()

    assert settings.workspace_dir == original_workspace_dir


def test_runtime_eval_scenarios_expose_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "provider_fallback_chain",
                "provider_health_reroute",
                "local_runtime_profile",
                "helper_local_runtime_paths",
                "context_window_summary_audit",
                "rest_chat_behavior",
                "rest_chat_approval_contract",
                "rest_chat_timeout_contract",
                "websocket_chat_behavior",
                "websocket_chat_approval_contract",
                "websocket_chat_timeout_contract",
                "strategist_tick_behavior",
                "strategist_tick_learning_continuity_behavior",
                "guardian_state_synthesis",
                "guardian_world_model_behavior",
                "guardian_judgment_behavior",
                "guardian_long_horizon_learning_behavior",
                "observer_refresh_behavior",
                "observer_delivery_decision_behavior",
                "native_presence_notification_behavior",
                "native_desktop_shell_behavior",
                "cross_surface_notification_controls_behavior",
                "cross_surface_continuity_behavior",
                "intervention_policy_behavior",
                "salience_calibration_behavior",
                "observer_delivery_salience_confidence_behavior",
                "guardian_feedback_loop",
                "guardian_outcome_learning",
                "guardian_learning_policy_v2_behavior",
                "agent_local_runtime_profile",
                "delegation_local_runtime_profile",
                "delegation_secret_boundary_behavior",
                "delegated_tool_workflow_behavior",
                "delegated_tool_workflow_degraded_behavior",
                "workflow_composition_behavior",
                "workflow_approval_threading_behavior",
                "threaded_operator_timeline_behavior",
                "background_session_handoff_behavior",
                "workflow_context_condenser_behavior",
                "workflow_operating_layer_behavior",
                "engineering_memory_bundle_behavior",
                "operator_continuity_graph_behavior",
                "workflow_boundary_blocked_surface_behavior",
                "approval_explainability_surface_behavior",
                "source_adapter_evidence_behavior",
                "source_mutation_boundary_behavior",
                "source_review_routine_behavior",
                "source_report_action_workflow_behavior",
                "capability_repair_behavior",
                "capability_preflight_behavior",
                "activity_ledger_attribution_behavior",
                "imported_capability_surface_behavior",
                "mcp_specialist_local_runtime_profile",
                "embedding_runtime_audit",
                "vector_store_runtime_audit",
                "soul_runtime_audit",
                "filesystem_runtime_audit",
                "vault_runtime_audit",
                "runtime_model_overrides",
                "runtime_fallback_overrides",
                "runtime_profile_preferences",
                "runtime_path_patterns",
                "provider_policy_capabilities",
                "provider_policy_scoring",
                "provider_policy_safeguards",
                "provider_routing_decision_audit",
                "session_bound_llm_trace",
                "session_consolidation_behavior",
                "memory_commitment_continuity_behavior",
                "memory_collaborator_lookup_behavior",
                "memory_engineering_retrieval_benchmark_behavior",
                "memory_contradiction_ranking_behavior",
                "memory_selective_forgetting_surface_behavior",
                "operator_memory_benchmark_surface_behavior",
                "memory_provider_user_model_behavior",
                "memory_provider_stale_evidence_behavior",
                "memory_provider_writeback_behavior",
                "bounded_memory_snapshot_behavior",
                "memory_supersession_filter_behavior",
                "memory_decay_contradiction_cleanup_behavior",
                "memory_reconciliation_policy_behavior",
                "procedural_memory_adaptation_behavior",
                "scheduled_local_runtime_profile",
                "process_recovery_boundary_behavior",
                "daily_briefing_delivery_behavior",
                "shell_tool_runtime_audit",
                "browser_runtime_audit",
                "web_search_runtime_audit",
                "web_search_empty_result_audit",
                "observer_calendar_source_audit",
                "observer_git_source_audit",
                "observer_goal_source_audit",
                "observer_time_source_audit",
                "observer_delivery_gate_audit",
                "observer_delivery_transport_audit",
                "observer_daemon_ingest_audit",
                "mcp_test_api_audit",
                "skills_api_audit",
                "tool_policy_guardrails_behavior",
                "screen_repository_runtime_audit",
                "daily_briefing_degraded_memories_audit",
                "activity_digest_degraded_delivery_behavior",
                "activity_digest_degraded_summary_audit",
                "evening_review_degraded_delivery_behavior",
                "evening_review_degraded_inputs_audit",
            ]
        )
    )

    assert summary.failed == 0
    details_by_name = {result.name: result.details for result in summary.results}

    assert details_by_name["provider_fallback_chain"]["attempted_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]
    assert details_by_name["provider_fallback_chain"]["final_model"] == "openai/gpt-4.1-mini"
    assert details_by_name["provider_health_reroute"]["attempted_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4o-mini",
    ]
    assert details_by_name["provider_health_reroute"]["rerouted_model"] == "openai/gpt-4o-mini"
    assert details_by_name["local_runtime_profile"]["runtime_profile"] == "local"
    assert details_by_name["local_runtime_profile"]["routed_model"] == "ollama/llama3.2"
    assert details_by_name["helper_local_runtime_paths"]["routed_models"]["context_window_summary"] == "ollama/llama3.2"
    assert details_by_name["helper_local_runtime_paths"]["routed_models"]["session_title_generation"] == "ollama/llama3.2"
    assert details_by_name["helper_local_runtime_paths"]["routed_models"]["session_consolidation"] == "ollama/llama3.2"
    assert details_by_name["context_window_summary_audit"]["success_model"] == "ollama/llama3.2"
    assert details_by_name["context_window_summary_audit"]["success_runtime_path"] == "context_window_summary"
    assert details_by_name["context_window_summary_audit"]["degraded_runtime_path"] == "context_window_summary"
    assert details_by_name["context_window_summary_audit"]["degraded_fallback"] == "truncation"
    assert details_by_name["context_window_summary_audit"]["degraded_contains_truncation"] is True
    assert details_by_name["rest_chat_behavior"]["first_status"] == 200
    assert details_by_name["rest_chat_behavior"]["second_status"] == 200
    assert details_by_name["rest_chat_behavior"]["session_reused"] is True
    assert details_by_name["rest_chat_behavior"]["first_response"] == "Hello from Seraph."
    assert details_by_name["rest_chat_behavior"]["follow_up_response"] == "Follow-up response"
    assert details_by_name["rest_chat_behavior"]["audit_transport"] == "rest"
    assert details_by_name["rest_chat_approval_contract"]["status_code"] == 409
    assert details_by_name["rest_chat_approval_contract"]["detail_type"] == "approval_required"
    assert details_by_name["rest_chat_approval_contract"]["approval_id"] == "approval-123"
    assert details_by_name["rest_chat_approval_contract"]["tool_name"] == "shell_execute"
    assert details_by_name["rest_chat_approval_contract"]["audit_summary_contains_shell"] is True
    assert details_by_name["rest_chat_timeout_contract"]["status_code"] == 504
    assert "timed out" in details_by_name["rest_chat_timeout_contract"]["detail"]
    assert details_by_name["rest_chat_timeout_contract"]["timeout_seconds"] == 120
    assert details_by_name["websocket_chat_behavior"]["welcome_type"] == "proactive"
    assert details_by_name["websocket_chat_behavior"]["skip_type"] == "final"
    assert details_by_name["websocket_chat_behavior"]["step_count"] >= 2
    assert details_by_name["websocket_chat_behavior"]["final_content"] == "It's sunny and 72°F today!"
    assert details_by_name["websocket_chat_behavior"]["seq_monotonic"] is True
    assert details_by_name["websocket_chat_behavior"]["audit_tool_call_count"] == 1
    assert details_by_name["websocket_chat_approval_contract"]["message_type"] == "approval_required"
    assert details_by_name["websocket_chat_approval_contract"]["approval_id"] == "approval-1"
    assert details_by_name["websocket_chat_approval_contract"]["tool_name"] == "shell_execute"
    assert details_by_name["websocket_chat_approval_contract"]["risk_level"] == "high"
    assert details_by_name["websocket_chat_approval_contract"]["audit_summary_contains_shell"] is True
    assert details_by_name["websocket_chat_timeout_contract"]["final_type"] == "final"
    assert details_by_name["websocket_chat_timeout_contract"]["final_contains_timeout_copy"] is True
    assert details_by_name["websocket_chat_timeout_contract"]["timeout_seconds"] == 120
    assert details_by_name["websocket_chat_timeout_contract"]["succeeded_event_present"] is False
    assert details_by_name["strategist_tick_behavior"]["message_type"] == "proactive"
    assert details_by_name["strategist_tick_behavior"]["intervention_type"] == "advisory"
    assert details_by_name["strategist_tick_behavior"]["urgency"] == 3
    assert details_by_name["strategist_tick_behavior"]["content_mentions_refocus"] is True
    assert details_by_name["strategist_tick_behavior"]["delivery"] == "deliver"
    assert details_by_name["strategist_tick_behavior"]["reasoning"] == "Focus drift"
    assert details_by_name["strategist_tick_learning_continuity_behavior"]["message_type"] == "proactive"
    assert details_by_name["strategist_tick_learning_continuity_behavior"]["urgency"] == 2
    assert details_by_name["strategist_tick_learning_continuity_behavior"]["scheduler_delivery"] == "queue"
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["scheduler_policy_action"]
        == "bundle"
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["policy_reason"]
        == "high_interruption_cost"
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["learning_bias"]
        == "neutral"
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["learning_channel_bias"]
        == "neutral"
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["transport"]
        is None
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["delivered_connections"]
        == 0
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["continuity_notification_count"]
        == 0
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["continuity_queued_insight_count"]
        == 1
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["continuity_surface"]
        == "bundle_queue"
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["continuity_excerpt_mentions_workflow"]
        is True
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["notification_intervention_matches"]
        is False
    )
    assert (
        details_by_name["strategist_tick_learning_continuity_behavior"]["remaining_notifications_before_cleanup"]
        == 0
    )
    assert details_by_name["guardian_state_synthesis"]["overall_confidence"] == "partial"
    assert details_by_name["guardian_state_synthesis"]["observer_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["world_model_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["observer_salience"] == "high"
    assert details_by_name["guardian_state_synthesis"]["observer_interruption_cost"] == "low"
    assert details_by_name["guardian_state_synthesis"]["world_model_focus"] == "Ship guardian state while in VS Code"
    assert details_by_name["guardian_state_synthesis"]["world_model_alignment"] == "medium"
    assert details_by_name["guardian_state_synthesis"]["world_model_memory_signals"] == 0
    assert details_by_name["guardian_state_synthesis"]["memory_confidence"] == "degraded"
    assert details_by_name["guardian_state_synthesis"]["current_session_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["recent_sessions_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["goal_summary"] == "Ship guardian state"
    assert details_by_name["guardian_state_synthesis"]["recent_sessions_contains_title"] is True
    assert details_by_name["guardian_state_synthesis"]["current_history_mentions_guardian_state"] is True
    assert details_by_name["guardian_state_synthesis"]["instructions_include_guardian_state"] is True
    assert details_by_name["guardian_world_model_behavior"]["world_model_confidence"] == "partial"
    assert details_by_name["guardian_world_model_behavior"]["current_focus"] == "Prepare investor brief while in Arc"
    assert details_by_name["guardian_world_model_behavior"]["focus_source"] == "observer_goal_window"
    assert details_by_name["guardian_world_model_behavior"]["focus_alignment"] == "high"
    assert details_by_name["guardian_world_model_behavior"]["intervention_receptivity"] == "low"
    assert "Recent intervention friction is present" in details_by_name["guardian_world_model_behavior"]["active_blockers"]
    assert "Workflow investor-brief degraded at write_file" in details_by_name["guardian_world_model_behavior"]["active_blockers"]
    assert (
        "Follow-through risk remains open for live project 'Investor brief'"
        in details_by_name["guardian_world_model_behavior"]["active_blockers"]
    )
    assert (
        details_by_name["guardian_world_model_behavior"]["next_up"][0].startswith(
            'Investor brief follow-up: assistant said "Close the investor brief loop before tomorrow."'
        )
    )
    assert "Investor brief" in details_by_name["guardian_world_model_behavior"]["next_up"]
    assert len(details_by_name["guardian_world_model_behavior"]["next_up"]) == 2
    assert (
        details_by_name["guardian_world_model_behavior"]["dominant_thread"].startswith("Investor brief follow-up")
    )
    assert details_by_name["guardian_world_model_behavior"]["active_commitments_count"] >= 2
    assert details_by_name["guardian_world_model_behavior"]["active_projects_count"] >= 1
    assert details_by_name["guardian_world_model_behavior"]["includes_investor_sync"] is True
    assert details_by_name["guardian_world_model_behavior"]["includes_investor_project"] is True
    assert details_by_name["guardian_world_model_behavior"]["includes_memory_signal"] is False
    assert details_by_name["guardian_world_model_behavior"]["includes_attention_pressure"] is True
    assert details_by_name["guardian_world_model_behavior"]["includes_feedback_pressure"] is True
    assert details_by_name["guardian_world_model_behavior"]["includes_execution_pressure"] is True
    assert details_by_name["guardian_world_model_behavior"]["continuity_thread_matches_live_project"] is True
    assert details_by_name["guardian_world_model_behavior"]["includes_follow_through_risk"] is True
    assert details_by_name["guardian_world_model_behavior"]["project_ranking_diagnostics_count"] >= 1
    assert details_by_name["guardian_world_model_behavior"]["includes_project_ranking_diagnostics"] is True
    assert details_by_name["guardian_world_model_behavior"]["user_model_confidence"] == "grounded"
    assert details_by_name["guardian_world_model_behavior"]["user_model_signal_count"] >= 2
    assert details_by_name["guardian_world_model_behavior"]["preference_inference_diagnostics_count"] >= 2
    assert details_by_name["guardian_world_model_behavior"]["has_guarded_async_user_model_signal"] is True
    assert details_by_name["guardian_world_model_behavior"]["has_brief_literal_user_model_signal"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_learning_diagnostics"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_world_model"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_focus"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_projects"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_active_blockers"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_next_up"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_dominant_thread"] is True
    assert details_by_name["guardian_world_model_behavior"]["agent_instructions_include_memory_signals"] is False
    assert details_by_name["guardian_world_model_behavior"]["strategist_instructions_include_receptivity"] is True
    assert details_by_name["guardian_judgment_behavior"]["overall_confidence"] == "partial"
    assert details_by_name["guardian_judgment_behavior"]["world_model_confidence"] == "degraded"
    assert details_by_name["guardian_judgment_behavior"]["focus_source"] == "observer_goal_window"
    assert details_by_name["guardian_judgment_behavior"]["judgment_risk_count"] >= 1
    assert details_by_name["guardian_judgment_behavior"]["includes_project_mismatch"] is True
    assert details_by_name["guardian_judgment_behavior"]["includes_supporting_context_mismatch"] is True
    assert details_by_name["guardian_judgment_behavior"]["includes_execution_context_mismatch"] is True
    assert details_by_name["guardian_judgment_behavior"]["dominant_thread_prefers_hermes"] is True
    assert details_by_name["guardian_judgment_behavior"]["project_state_includes_hermes_execution"] is True
    assert details_by_name["guardian_judgment_behavior"]["includes_project_anchor_drift"] is True
    assert details_by_name["guardian_judgment_behavior"]["project_ranking_diagnostics_count"] >= 1
    assert details_by_name["guardian_judgment_behavior"]["stale_signal_arbitration_count"] >= 1
    assert details_by_name["guardian_judgment_behavior"]["includes_ranked_project_diagnostics"] is True
    assert details_by_name["guardian_judgment_behavior"]["includes_stale_signal_arbitration"] is True
    assert details_by_name["guardian_judgment_behavior"]["includes_conservative_ambiguity_guardrail"] is True
    assert details_by_name["guardian_judgment_behavior"]["has_learning_conflict_diagnostic"] is True
    assert details_by_name["guardian_judgment_behavior"]["has_live_override_diagnostic"] is True
    assert details_by_name["guardian_judgment_behavior"]["user_model_confidence"] == "grounded"
    assert details_by_name["guardian_judgment_behavior"]["user_model_signal_count"] >= 2
    assert details_by_name["guardian_judgment_behavior"]["has_user_model_signal"] is True
    assert details_by_name["guardian_judgment_behavior"]["has_user_model_sources_diagnostic"] is True
    assert (
        details_by_name["guardian_judgment_behavior"]["ambiguous_request_intent_uncertainty_level"]
        == "high"
    )
    assert (
        details_by_name["guardian_judgment_behavior"]["ambiguous_request_resolution"]
        == "clarify"
    )
    assert (
        details_by_name["guardian_judgment_behavior"]["ambiguous_request_has_referent_diagnostic"]
        is True
    )
    assert (
        details_by_name["guardian_judgment_behavior"]["ambiguous_request_has_project_anchor_diagnostic"]
        is True
    )
    assert (
        details_by_name["guardian_judgment_behavior"]["ambiguous_request_prompt_includes_intent_uncertainty"]
        is True
    )
    assert details_by_name["guardian_judgment_behavior"]["judgment_proof_count"] >= 1
    assert details_by_name["guardian_judgment_behavior"]["has_project_target_proof"] is True
    assert details_by_name["guardian_judgment_behavior"]["has_referent_proof"] is True
    assert details_by_name["guardian_judgment_behavior"]["prompt_includes_judgment_proof"] is True
    assert (
        details_by_name["guardian_judgment_behavior"]["split_preference_has_interaction_style_proof"]
        is True
    )
    assert (
        details_by_name["guardian_judgment_behavior"]["split_preference_has_observer_proof"]
        is True
    )
    assert (
        details_by_name["guardian_judgment_behavior"]["split_preference_prompt_includes_judgment_proof"]
        is True
    )
    assert details_by_name["guardian_judgment_behavior"]["decision_action"] == "defer"
    assert details_by_name["guardian_judgment_behavior"]["decision_reason"] == "low_guardian_confidence"
    assert details_by_name["guardian_long_horizon_learning_behavior"]["multi_day_negative_days"] == 3
    assert details_by_name["guardian_long_horizon_learning_behavior"]["scheduled_negative_days"] == 2
    assert details_by_name["guardian_long_horizon_learning_behavior"]["intervention_receptivity"] == "low"
    assert details_by_name["guardian_long_horizon_learning_behavior"]["abstention_action"] == "defer"
    assert details_by_name["guardian_long_horizon_learning_behavior"]["abstention_reason"] == "long_horizon_negative_trend"
    assert details_by_name["guardian_long_horizon_learning_behavior"]["scheduled_action"] == "defer"
    assert details_by_name["guardian_long_horizon_learning_behavior"]["scheduled_reason"] == "scheduled_outcome_instability"
    assert details_by_name["guardian_long_horizon_learning_behavior"]["learning_diagnostics_count"] >= 2
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_goal_alignment_signal"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_unstable_review_signal"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_routine_watchpoint"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_collaborator_watchpoint"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_multi_day_risk"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_abstention_risk"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_scheduled_deferral_risk"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_learning_scope_diagnostic"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_learning_spread_diagnostic"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_learning_abstention_diagnostic"] is True
    assert details_by_name["guardian_long_horizon_learning_behavior"]["has_learning_scheduled_diagnostic"] is True
    assert details_by_name["native_presence_notification_behavior"]["action"] == "act"
    assert details_by_name["native_presence_notification_behavior"]["delivery_decision"] == "deliver"
    assert details_by_name["native_presence_notification_behavior"]["transport"] == "native_notification"
    assert details_by_name["native_presence_notification_behavior"]["notification_title"] == "Seraph alert"
    assert details_by_name["native_presence_notification_behavior"]["notification_body_matches"] is True
    assert details_by_name["native_presence_notification_behavior"]["acked"] is True
    assert details_by_name["native_presence_notification_behavior"]["remaining_notifications"] == 0
    assert details_by_name["native_desktop_shell_behavior"]["initial_capture_mode"] == "balanced"
    assert details_by_name["native_desktop_shell_behavior"]["initial_pending_notifications"] == 0
    assert details_by_name["native_desktop_shell_behavior"]["queued_title"] == "Seraph desktop shell"
    assert details_by_name["native_desktop_shell_behavior"]["queued_pending_notifications"] == 1
    assert details_by_name["native_desktop_shell_behavior"]["queued_outcome"] == "queued_test"
    assert details_by_name["native_desktop_shell_behavior"]["acked"] is True
    assert details_by_name["native_desktop_shell_behavior"]["acked_pending_notifications"] == 0
    assert details_by_name["native_desktop_shell_behavior"]["acked_outcome"] == "acked"
    assert details_by_name["native_desktop_shell_behavior"]["queued_event_source"] == "test_endpoint"
    assert details_by_name["native_desktop_shell_behavior"]["ack_event_matches"] is True
    assert details_by_name["cross_surface_notification_controls_behavior"]["listed_before_pending_count"] == 2
    assert details_by_name["cross_surface_notification_controls_behavior"]["listed_before_titles"] == [
        "Seraph desktop shell",
        "Seraph desktop shell",
    ]
    assert details_by_name["cross_surface_notification_controls_behavior"]["dismissed_single"] is True
    assert details_by_name["cross_surface_notification_controls_behavior"]["listed_after_single_pending_count"] == 1
    assert details_by_name["cross_surface_notification_controls_behavior"]["dismissed_all_count"] == 1
    assert details_by_name["cross_surface_notification_controls_behavior"]["final_pending_count"] == 0
    assert details_by_name["cross_surface_notification_controls_behavior"]["final_outcome"] == "dismissed"
    assert details_by_name["cross_surface_notification_controls_behavior"]["dismiss_event_source"] == "browser_controls"
    assert details_by_name["cross_surface_notification_controls_behavior"]["dismiss_all_event_source"] == "browser_controls"
    assert details_by_name["cross_surface_notification_controls_behavior"]["dismiss_all_event_count"] == 1
    assert details_by_name["cross_surface_notification_controls_behavior"]["second_notification_preserved_until_bulk_clear"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["daemon_pending_notifications"] == 1
    assert details_by_name["cross_surface_continuity_behavior"]["notification_count"] == 1
    assert details_by_name["cross_surface_continuity_behavior"]["notification_intervention_matches"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["notification_continuation_mode"] == "resume_thread"
    assert details_by_name["cross_surface_continuity_behavior"]["notification_thread_id"] == "continuity-session"
    assert details_by_name["cross_surface_continuity_behavior"]["queued_insight_count"] == 1
    assert details_by_name["cross_surface_continuity_behavior"]["queued_bundle_matches"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["queued_continuation_mode"] == "resume_thread"
    assert details_by_name["cross_surface_continuity_behavior"]["queued_thread_id"] == "continuity-session"
    assert details_by_name["cross_surface_continuity_behavior"]["recent_continuation_mode"] == "resume_thread"
    assert details_by_name["cross_surface_continuity_behavior"]["recent_thread_id"] == "continuity-session"
    assert details_by_name["cross_surface_continuity_behavior"]["live_route_status"] == "fallback_active"
    assert details_by_name["cross_surface_continuity_behavior"]["live_route_transport"] == "native_notification"
    assert details_by_name["cross_surface_continuity_behavior"]["native_surface_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["bundle_surface_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["degraded_source_adapter_count"] == 1
    assert details_by_name["cross_surface_continuity_behavior"]["attention_family_count"] == 1
    assert details_by_name["cross_surface_continuity_behavior"]["presence_surface_count"] == 4
    assert details_by_name["cross_surface_continuity_behavior"]["attention_presence_surface_count"] == 2
    assert details_by_name["cross_surface_continuity_behavior"]["source_adapter_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["presence_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["presence_follow_up_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["browser_provider_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["node_adapter_follow_up_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["imported_reach_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["operator_source_adapter_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["operator_presence_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["operator_imported_reach_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["operator_presence_follow_up_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["activity_source_adapter_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["activity_presence_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["activity_imported_reach_recovery_present"] is True
    assert details_by_name["cross_surface_continuity_behavior"]["activity_presence_follow_up_present"] is True
    assert details_by_name["guardian_state_synthesis"]["instructions_include_recent_sessions"] is True
    assert details_by_name["observer_refresh_behavior"]["new_user_state"] == "transitioning"
    assert details_by_name["observer_refresh_behavior"]["data_quality"] == "good"
    assert details_by_name["observer_refresh_behavior"]["observer_confidence"] == "grounded"
    assert details_by_name["observer_refresh_behavior"]["salience_level"] == "high"
    assert details_by_name["observer_refresh_behavior"]["salience_reason"] == "upcoming_event"
    assert details_by_name["observer_refresh_behavior"]["interruption_cost"] == "high"
    assert details_by_name["observer_refresh_behavior"]["screen_context_preserved"] is True
    assert details_by_name["observer_refresh_behavior"]["active_window_preserved"] is True
    assert details_by_name["observer_refresh_behavior"]["goal_summary"] == "Ship guardian behavioral evals"
    assert details_by_name["observer_refresh_behavior"]["upcoming_event_count"] == 1
    assert details_by_name["observer_refresh_behavior"]["triggered_bundle_delivery"] is True
    assert details_by_name["observer_refresh_behavior"]["bundle_task_scheduled"] is True
    assert details_by_name["observer_delivery_decision_behavior"]["delivered_decision"] == "deliver"
    assert details_by_name["observer_delivery_decision_behavior"]["delivered_action"] == "act"
    assert details_by_name["observer_delivery_decision_behavior"]["queued_decision"] == "queue"
    assert details_by_name["observer_delivery_decision_behavior"]["queued_action"] == "bundle"
    assert details_by_name["observer_delivery_decision_behavior"]["budget_decremented"] is True
    assert details_by_name["observer_delivery_decision_behavior"]["queued_content_matches"] is True
    assert details_by_name["observer_delivery_decision_behavior"]["delivered_connections"] == 1
    assert details_by_name["observer_delivery_decision_behavior"]["queued_user_state"] == "deep_work"
    assert details_by_name["intervention_policy_behavior"]["act_action"] == "act"
    assert details_by_name["intervention_policy_behavior"]["act_reason"] == "available_capacity"
    assert details_by_name["intervention_policy_behavior"]["bundle_action"] == "bundle"
    assert details_by_name["intervention_policy_behavior"]["bundle_reason"] == "blocked_state"
    assert details_by_name["intervention_policy_behavior"]["defer_action"] == "defer"
    assert details_by_name["intervention_policy_behavior"]["defer_reason"] == "low_guardian_confidence"
    assert details_by_name["intervention_policy_behavior"]["approval_action"] == "request_approval"
    assert details_by_name["intervention_policy_behavior"]["approval_reason"] == "requires_approval"
    assert details_by_name["intervention_policy_behavior"]["silent_action"] == "stay_silent"
    assert details_by_name["intervention_policy_behavior"]["silent_reason"] == "empty_content"
    assert details_by_name["intervention_policy_behavior"]["high_interrupt_action"] == "bundle"
    assert details_by_name["intervention_policy_behavior"]["high_interrupt_reason"] == "high_interruption_cost"
    assert details_by_name["intervention_policy_behavior"]["low_salience_action"] == "stay_silent"
    assert details_by_name["intervention_policy_behavior"]["low_salience_reason"] == "low_observer_salience"
    assert details_by_name["intervention_policy_behavior"]["learned_bundle_action"] == "bundle"
    assert details_by_name["intervention_policy_behavior"]["learned_bundle_reason"] == "recent_negative_feedback"
    assert details_by_name["salience_calibration_behavior"]["aligned_work_salience_level"] == "high"
    assert details_by_name["salience_calibration_behavior"]["aligned_work_salience_reason"] == "aligned_work_activity"
    assert details_by_name["salience_calibration_behavior"]["single_goal_salience_level"] == "medium"
    assert details_by_name["salience_calibration_behavior"]["single_goal_salience_reason"] == "active_goals"
    assert details_by_name["salience_calibration_behavior"]["calibrated_action"] == "act"
    assert details_by_name["salience_calibration_behavior"]["calibrated_reason"] == "calibrated_high_salience"
    assert details_by_name["salience_calibration_behavior"]["focus_mode_action"] == "bundle"
    assert details_by_name["salience_calibration_behavior"]["focus_mode_reason"] == "high_interruption_cost"
    assert details_by_name["salience_calibration_behavior"]["low_observer_confidence_action"] == "defer"
    assert details_by_name["salience_calibration_behavior"]["low_observer_confidence_reason"] == "low_observer_confidence"
    assert details_by_name["salience_calibration_behavior"]["degraded_state_action"] == "defer"
    assert details_by_name["salience_calibration_behavior"]["degraded_state_reason"] == "degraded_observer_state"
    assert details_by_name["salience_calibration_behavior"]["urgent_override_action"] == "act"
    assert details_by_name["salience_calibration_behavior"]["urgent_override_reason"] == "urgent"
    assert details_by_name["salience_calibration_behavior"]["scheduled_override_action"] == "act"
    assert details_by_name["salience_calibration_behavior"]["scheduled_override_reason"] == "scheduled"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_action"] == "act"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_reason"] == "calibrated_high_salience"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_delivery_decision"] == "deliver"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_transport"] == "websocket"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_delivered_connections"] == 1
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_budget_decremented"] is True
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_observer_confidence"] == "grounded"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_salience_reason"] == "aligned_work_activity"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["calibrated_interruption_cost"] == "high"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_action"] == "defer"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_reason"] == "low_observer_confidence"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_delivery_decision"] is None
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_observer_confidence"] == "degraded"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_salience_reason"] == "aligned_work_activity"
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_transport_present"] is False
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_broadcast_skipped"] is True
    assert details_by_name["observer_delivery_salience_confidence_behavior"]["degraded_queue_skipped"] is True
    assert details_by_name["guardian_feedback_loop"]["action"] == "act"
    assert details_by_name["guardian_feedback_loop"]["delivery_decision"] == "deliver"
    assert details_by_name["guardian_feedback_loop"]["delivery_transport"] == "native_notification"
    assert details_by_name["guardian_feedback_loop"]["intervention_id_present"] is True
    assert details_by_name["guardian_feedback_loop"]["notification_intervention_matches"] is True
    assert details_by_name["guardian_feedback_loop"]["acked"] is True
    assert details_by_name["guardian_feedback_loop"]["feedback_recorded"] is True
    assert details_by_name["guardian_feedback_loop"]["ack_event_matches"] is True
    assert details_by_name["guardian_feedback_loop"]["feedback_type"] == "helpful"
    assert details_by_name["guardian_feedback_loop"]["latest_outcome"] == "feedback_received"
    assert details_by_name["guardian_feedback_loop"]["stored_feedback_type"] == "helpful"
    assert details_by_name["guardian_feedback_loop"]["summary_contains_feedback"] is True
    assert details_by_name["guardian_feedback_loop"]["summary_mentions_excerpt"] is True
    assert details_by_name["guardian_feedback_loop"]["prompt_contains_feedback_section"] is True
    assert details_by_name["guardian_feedback_loop"]["instructions_include_feedback"] is True
    assert details_by_name["guardian_feedback_loop"]["remaining_notifications"] == 0
    assert details_by_name["guardian_outcome_learning"]["action"] == "defer"
    assert details_by_name["guardian_outcome_learning"]["reason"] == "learned_suppression_window"
    assert details_by_name["guardian_outcome_learning"]["queued"] is False
    assert details_by_name["guardian_outcome_learning"]["broadcast_skipped"] is True
    assert details_by_name["guardian_outcome_learning"]["learning_bias"] == "neutral"
    assert details_by_name["guardian_outcome_learning"]["learning_not_helpful_count"] == 2
    assert details_by_name["guardian_outcome_learning"]["positive_action"] == "bundle"
    assert details_by_name["guardian_outcome_learning"]["positive_reason"] == "high_interruption_cost"
    assert details_by_name["guardian_outcome_learning"]["positive_transport"] is None
    assert details_by_name["guardian_outcome_learning"]["positive_learning_bias"] == "neutral"
    assert (
        details_by_name["guardian_outcome_learning"]["positive_learning_channel_bias"]
        == "neutral"
    )
    assert details_by_name["guardian_outcome_learning"]["positive_helpful_count"] == 2
    assert details_by_name["guardian_outcome_learning"]["positive_acknowledged_count"] == 2
    assert details_by_name["guardian_outcome_learning"]["remaining_native_notifications"] == 0
    assert details_by_name["guardian_learning_policy_v2_behavior"]["timing_bias"] == "avoid_focus_windows"
    assert details_by_name["guardian_learning_policy_v2_behavior"]["blocked_state_bias"] == "avoid_blocked_state_interruptions"
    assert details_by_name["guardian_learning_policy_v2_behavior"]["blocked_action"] == "bundle"
    assert details_by_name["guardian_learning_policy_v2_behavior"]["blocked_reason"] == "learned_blocked_state_avoidance"
    assert details_by_name["guardian_learning_policy_v2_behavior"]["available_action"] == "act"
    assert details_by_name["guardian_learning_policy_v2_behavior"]["available_reason"] == "learned_available_window"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["chat_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["onboarding_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["strategist_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["memory_keeper"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["orchestrator_agent"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["vault_keeper"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["goal_planner"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["web_researcher"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["file_worker"] == "ollama/llama3.2"
    assert details_by_name["delegation_secret_boundary_behavior"]["memory_excludes_secret_tools"] is True
    assert details_by_name["delegation_secret_boundary_behavior"]["vault_only_secret_tools"] is True
    assert details_by_name["delegation_secret_boundary_behavior"]["secret_task_routed_to_vault_keeper"] is True
    assert details_by_name["delegation_secret_boundary_behavior"]["memory_task_routed_to_memory_keeper"] is True
    assert details_by_name["delegation_secret_boundary_behavior"]["secret_task_result"] == "vault handled"
    assert details_by_name["delegation_secret_boundary_behavior"]["memory_task_result"] == "memory handled"
    assert details_by_name["delegation_secret_boundary_behavior"]["explicit_vault_alias_result"] == "vault handled"
    assert details_by_name["delegated_tool_workflow_behavior"]["delegated_to_web_researcher"] is True
    assert details_by_name["delegated_tool_workflow_behavior"]["delegated_to_file_worker"] is True
    assert details_by_name["delegated_tool_workflow_behavior"]["tool_steps_present"] == {
        "browse_webpage": True,
        "web_search": True,
        "write_file": True,
    }
    assert details_by_name["delegated_tool_workflow_behavior"]["final_mentions_saved_plan"] is True
    assert details_by_name["delegated_tool_workflow_behavior"]["audit_result_tools"] == [
        "browse_webpage",
        "web_search",
        "write_file",
    ]
    assert details_by_name["delegated_tool_workflow_behavior"]["saved_plan_mentions_provider_policy"] is True
    assert details_by_name["delegated_tool_workflow_behavior"]["tool_call_count"] == 5
    assert details_by_name["delegated_tool_workflow_degraded_behavior"]["delegated_to_web_researcher"] is True
    assert details_by_name["delegated_tool_workflow_degraded_behavior"]["web_search_failed_audited"] is False
    assert details_by_name["delegated_tool_workflow_degraded_behavior"]["browse_failed_audited"] is True
    assert details_by_name["delegated_tool_workflow_degraded_behavior"]["write_file_still_succeeded"] is True
    assert details_by_name["delegated_tool_workflow_degraded_behavior"]["final_mentions_fallback"] is True
    assert details_by_name["delegated_tool_workflow_degraded_behavior"]["saved_plan_mentions_incident_trace"] is True
    assert details_by_name["delegated_tool_workflow_degraded_behavior"]["tool_call_count"] == 5
    assert details_by_name["workflow_composition_behavior"]["without_skill_tool_names"] == [
        "workflow_web_brief_to_file",
    ]
    assert details_by_name["workflow_composition_behavior"]["with_skill_tool_names"] == [
        "workflow_goal_snapshot_to_file",
        "workflow_web_brief_to_file",
    ]
    assert details_by_name["workflow_composition_behavior"]["web_search_called_with"] == "workflow composition"
    assert details_by_name["workflow_composition_behavior"]["saved_file_path"] == "notes/workflow.md"
    assert details_by_name["workflow_composition_behavior"]["saved_content_contains_search"] is True
    assert details_by_name["workflow_composition_behavior"]["result"] == (
        "Saved search results for workflow composition to notes/workflow.md."
    )
    assert details_by_name["workflow_composition_behavior"]["workflow_runner_present"] is True
    assert details_by_name["workflow_composition_behavior"]["workflow_runner_tool_names"] == [
        "workflow_web_brief_to_file",
    ]
    assert details_by_name["workflow_approval_threading_behavior"]["status"] == "awaiting_approval"
    assert details_by_name["workflow_approval_threading_behavior"]["thread_label"] == "Research thread"
    assert details_by_name["workflow_approval_threading_behavior"]["thread_source"] == "session"
    assert details_by_name["workflow_approval_threading_behavior"]["pending_approval_count"] == 1
    assert details_by_name["workflow_approval_threading_behavior"]["pending_resume_message"] == "Continue once the web brief is approved"
    assert details_by_name["workflow_approval_threading_behavior"]["timeline_has_approval"] is True
    assert details_by_name["workflow_approval_threading_behavior"]["replay_allowed"] is False
    assert details_by_name["workflow_approval_threading_behavior"]["replay_block_reason"] == "pending_approval"
    assert details_by_name["workflow_approval_threading_behavior"]["replay_draft_is_none"] is True
    assert details_by_name["workflow_approval_threading_behavior"]["replay_input_keys"] == ["file_path", "query"]
    assert details_by_name["workflow_approval_threading_behavior"]["parameter_schema_keys"] == [
        "file_path",
        "query",
    ]
    assert details_by_name["workflow_approval_threading_behavior"]["replay_recommended_actions"] == [
        "open_settings",
    ]
    assert details_by_name["workflow_approval_threading_behavior"]["resume_from_step"] == "approval_gate"
    assert details_by_name["workflow_approval_threading_behavior"]["resume_checkpoint_label"] == "Approval gate"
    assert details_by_name["workflow_approval_threading_behavior"]["branch_kind"] == "approval_resume"
    assert (
        details_by_name["workflow_approval_threading_behavior"]["root_run_identity_matches_source"]
        is True
    )
    assert details_by_name["workflow_approval_threading_behavior"]["checkpoint_candidate_kinds"] == [
        "approval_gate",
    ]
    assert details_by_name["workflow_approval_threading_behavior"]["resume_plan_branch_kind"] == (
        "approval_resume"
    )
    assert (
        details_by_name["workflow_approval_threading_behavior"]["resume_plan_requires_manual_execution"]
        is True
    )
    assert (
        details_by_name["workflow_approval_threading_behavior"]["thread_continue_message"]
        == "Continue once the web brief is approved"
    )
    assert (
        details_by_name["workflow_approval_threading_behavior"]["approval_recovery_message"]
        == "Review pending approval(s) for workflow 'web-brief-to-file' before replaying."
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["item_kinds"] == [
        "notification",
        "intervention",
        "queued_insight",
        "workflow_run",
        "approval",
        "audit",
    ]
    assert details_by_name["threaded_operator_timeline_behavior"]["latest_kind"] == "notification"
    assert details_by_name["threaded_operator_timeline_behavior"]["workflow_thread_id"] == "thread-1"
    assert (
        details_by_name["threaded_operator_timeline_behavior"]["workflow_continue_message_matches_retry_plan"]
        is True
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["workflow_replay_allowed"] is True
    assert details_by_name["threaded_operator_timeline_behavior"]["workflow_resume_from_step"] == "write_step"
    assert (
        details_by_name["threaded_operator_timeline_behavior"]["workflow_resume_checkpoint_label"]
        == "Retry failed step"
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["workflow_run_identity"] == (
        "thread-1:workflow_web_brief_to_file:web-brief"
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["workflow_branch_kind"] == (
        "retry_failed_step"
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["workflow_resume_plan_kind"] == (
        "retry_failed_step"
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["workflow_failed_step_tool"] == (
        "write_file"
    )
    assert (
        details_by_name["threaded_operator_timeline_behavior"]["workflow_checkpoint_candidate_count"]
        == 2
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["approval_thread_matches"] is True
    assert (
        details_by_name["threaded_operator_timeline_behavior"]["approval_continue_message"]
        == "Continue after shell approval."
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["notification_thread_matches"] is True
    assert (
        details_by_name["threaded_operator_timeline_behavior"]["notification_continue_message"]
        == "Continue from native notification."
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["queued_thread_matches"] is True
    assert (
        details_by_name["threaded_operator_timeline_behavior"]["queued_continue_message"]
        == "Continue from queued guardian bundle: Bundle the research notes for later."
    )
    assert details_by_name["threaded_operator_timeline_behavior"]["intervention_source"] == "native_notification"
    assert details_by_name["threaded_operator_timeline_behavior"]["audit_thread_label"] == "Research thread"
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["operator_continue_message_mentions_boundary"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["operator_replay_draft_is_none"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["operator_recommended_actions_empty"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["operator_resume_plan_is_none"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["operator_checkpoint_candidates_empty"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["activity_continue_message_mentions_boundary"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["activity_replay_draft_is_none"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["activity_recommended_actions_empty"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["activity_resume_plan_is_none"] is True
    assert details_by_name["workflow_boundary_blocked_surface_behavior"]["activity_checkpoint_candidates_empty"] is True
    assert details_by_name["approval_explainability_surface_behavior"][
        "pending_requires_lifecycle_approval"
    ] is True
    assert (
        details_by_name["approval_explainability_surface_behavior"]["pending_scope_target_reference"]
        == "workflows/write-note.md"
    )
    assert (
        details_by_name["approval_explainability_surface_behavior"]["operator_scope_target_reference"]
        == "workflows/write-note.md"
    )
    assert (
        details_by_name["approval_explainability_surface_behavior"]["activity_scope_target_reference"]
        == "workflows/write-note.md"
    )
    assert (
        details_by_name["approval_explainability_surface_behavior"]["activity_extension_action"]
        == "source_save"
    )
    assert details_by_name["source_adapter_evidence_behavior"]["adapter_count"] >= 4
    assert details_by_name["source_adapter_evidence_behavior"]["ready_adapter_count"] >= 3
    assert details_by_name["source_adapter_evidence_behavior"]["github_adapter_state"] == "ready"
    assert details_by_name["source_adapter_evidence_behavior"]["github_work_item_tool"] == "search_issues"
    assert details_by_name["source_adapter_evidence_behavior"]["search_status"] == "ok"
    assert details_by_name["source_adapter_evidence_behavior"]["search_item_kind"] == "search_result"
    assert details_by_name["source_adapter_evidence_behavior"]["page_status"] == "ok"
    assert details_by_name["source_adapter_evidence_behavior"]["page_item_kind"] == "webpage"
    assert details_by_name["source_adapter_evidence_behavior"]["session_status"] == "ok"
    assert details_by_name["source_adapter_evidence_behavior"]["session_item_kind"] == "browser_snapshot"
    assert details_by_name["source_adapter_evidence_behavior"]["github_bundle_status"] == "ok"
    assert details_by_name["source_adapter_evidence_behavior"]["github_item_kind"] == "work_item"
    assert details_by_name["source_adapter_evidence_behavior"]["github_runtime_server"] == "github"
    assert details_by_name["source_adapter_evidence_behavior"]["overview_source_adapters_total"] >= 4
    assert details_by_name["source_adapter_evidence_behavior"]["overview_source_adapters_ready"] >= 3
    assert details_by_name["source_review_routine_behavior"]["daily_plan_status"] == "ready"
    assert details_by_name["source_review_routine_behavior"]["daily_ready_step_count"] >= 4
    assert details_by_name["source_review_routine_behavior"]["daily_work_items_source"] == "github-managed"
    assert details_by_name["source_review_routine_behavior"]["daily_code_activity_source"] == "github-managed"
    assert details_by_name["source_review_routine_behavior"]["daily_context_source"] == "web_search"
    assert details_by_name["source_review_routine_behavior"]["daily_explicit_page_source"] == "browse_webpage"
    assert details_by_name["source_review_routine_behavior"]["goal_plan_status"] == "ready"
    assert details_by_name["source_review_routine_behavior"]["goal_runbook"] == "runbook:source-goal-alignment"
    assert details_by_name["source_review_routine_behavior"]["goal_starter_pack"] == "source-goal-alignment"
    assert details_by_name["source_mutation_boundary_behavior"]["ready_status"] == "approval_required"
    assert details_by_name["source_mutation_boundary_behavior"]["ready_requires_approval"] is True
    assert (
        details_by_name["source_mutation_boundary_behavior"]["ready_scope_reference"]
        == "seraph-quest/seraph#342"
    )
    assert details_by_name["source_mutation_boundary_behavior"]["ready_field_count"] == 2
    assert details_by_name["source_mutation_boundary_behavior"]["ready_runtime_server"] == "github"
    assert details_by_name["source_mutation_boundary_behavior"]["ready_execution_boundaries"] == [
        "external_mcp",
        "authenticated_external_source",
        "connector_mutation",
    ]
    assert details_by_name["source_mutation_boundary_behavior"]["degraded_status"] == "degraded"
    assert details_by_name["source_mutation_boundary_behavior"]["degraded_warning_mentions_route"] is True
    assert details_by_name["source_mutation_boundary_behavior"]["degraded_route_executable"] is False
    assert details_by_name["source_report_action_workflow_behavior"]["report_status"] == "ready"
    assert details_by_name["source_report_action_workflow_behavior"]["publish_status"] == "approval_required"
    assert details_by_name["source_report_action_workflow_behavior"]["publish_action_kind"] == "comment"
    assert (
        details_by_name["source_report_action_workflow_behavior"]["publish_target_reference"]
        == "seraph-quest/seraph#343"
    )
    assert (
        details_by_name["source_report_action_workflow_behavior"]["recommended_runbook"]
        == "runbook:source-progress-report"
    )
    assert (
        details_by_name["source_report_action_workflow_behavior"]["recommended_starter_pack"]
        == "source-progress-report"
    )
    assert details_by_name["source_report_action_workflow_behavior"]["execution_status"] == "ok"
    assert (
        details_by_name["source_report_action_workflow_behavior"]["execution_tool_name"]
        == "add_comment_to_issue"
    )
    assert details_by_name["source_report_action_workflow_behavior"]["execution_argument_keys"] == [
        "comment",
        "issue_number",
        "repo_full_name",
    ]
    assert details_by_name["capability_repair_behavior"]["starter_pack_availability"] == "blocked"
    assert "set_tool_policy" in details_by_name["capability_repair_behavior"]["starter_pack_repair_actions"]
    assert "set_tool_policy" in details_by_name["capability_repair_behavior"]["workflow_repair_actions"]
    assert any(
        label.startswith("Allow write_file")
        for label in details_by_name["capability_repair_behavior"]["recommendation_labels"]
    )
    assert details_by_name["capability_repair_behavior"]["runbooks_ready"] >= 1
    assert details_by_name["capability_preflight_behavior"]["workflow_ready"] is False
    assert details_by_name["capability_preflight_behavior"]["workflow_can_autorepair"] is False
    assert details_by_name["capability_preflight_behavior"]["workflow_blocking_reasons"] == [
        "missing tool: write_file",
    ]
    assert details_by_name["capability_preflight_behavior"]["workflow_parameter_schema_keys"] == [
        "file_path",
        "query",
    ]
    assert details_by_name["capability_preflight_behavior"]["workflow_recommended_action_types"] == [
        "set_tool_policy",
    ]
    assert details_by_name["capability_preflight_behavior"]["workflow_autorepair_action_types"] == []
    assert details_by_name["capability_preflight_behavior"]["starter_pack_can_autorepair"] is False
    assert "skill web-briefing missing tool: write_file" in (
        details_by_name["capability_preflight_behavior"]["starter_pack_blocking_reasons"]
    )
    assert details_by_name["capability_preflight_behavior"]["starter_pack_command_present"] is True
    assert details_by_name["capability_preflight_behavior"]["starter_pack_autorepair_action_types"] == []
    assert details_by_name["capability_preflight_behavior"]["runbook_ready"] is False
    assert details_by_name["capability_preflight_behavior"]["runbook_can_autorepair"] is False
    assert details_by_name["capability_preflight_behavior"]["runbook_parameter_schema_keys"] == [
        "file_path",
        "query",
    ]
    assert details_by_name["capability_preflight_behavior"]["runbook_risk_level"] == "medium"
    assert details_by_name["capability_preflight_behavior"]["runbook_execution_boundaries"] == [
        "external_read",
        "workspace_write",
    ]
    assert details_by_name["capability_preflight_behavior"]["runbook_blocking_reasons"] == [
        "missing tool: write_file",
    ]
    assert details_by_name["capability_preflight_behavior"]["runbook_autorepair_action_types"] == []
    assert details_by_name["activity_ledger_attribution_behavior"]["runtime_path_bucket_keys"] == [
        "browser_agent",
        "chat_agent",
        "unattributed",
    ]
    assert details_by_name["activity_ledger_attribution_behavior"]["capability_family_bucket_keys"] == [
        "browser",
        "conversation",
        "unattributed",
    ]
    assert details_by_name["activity_ledger_attribution_behavior"]["chat_runtime_path"] == "chat_agent"
    assert details_by_name["activity_ledger_attribution_behavior"]["chat_budget_class"] == "medium"
    assert details_by_name["activity_ledger_attribution_behavior"]["browser_selected_source"] == "browser_provider"
    assert details_by_name["activity_ledger_attribution_behavior"]["unattributed_family"] == "unattributed"
    assert details_by_name["imported_capability_surface_behavior"]["catalog_extension_package_count"] >= 10
    assert details_by_name["imported_capability_surface_behavior"]["browser_provider_pack_count"] >= 1
    assert details_by_name["imported_capability_surface_behavior"]["messaging_connector_pack_count"] >= 1
    assert details_by_name["imported_capability_surface_behavior"]["automation_trigger_pack_count"] >= 1
    assert details_by_name["imported_capability_surface_behavior"]["node_adapter_pack_count"] >= 1
    assert details_by_name["imported_capability_surface_behavior"]["canvas_output_pack_count"] >= 1
    assert details_by_name["imported_capability_surface_behavior"]["workflow_runtime_pack_count"] >= 1
    assert details_by_name["imported_capability_surface_behavior"]["extension_payload_has_governance"] is True
    assert "channel_adapters" in details_by_name["imported_capability_surface_behavior"][
        "installed_extension_contribution_types"
    ]
    assert "managed_connectors" in details_by_name["imported_capability_surface_behavior"][
        "installed_extension_contribution_types"
    ]
    assert details_by_name["mcp_specialist_local_runtime_profile"]["runtime_path"] == "mcp_github_actions"
    assert details_by_name["mcp_specialist_local_runtime_profile"]["routed_model"] == "ollama/llama3.2"
    assert details_by_name["embedding_runtime_audit"]["loaded_integration_type"] == "embedding_model"
    assert details_by_name["embedding_runtime_audit"]["loaded_model"] == "all-MiniLM-L6-v2"
    assert details_by_name["embedding_runtime_audit"]["vector_length"] == 2
    assert details_by_name["embedding_runtime_audit"]["failure_stage"] == "encode"
    assert details_by_name["embedding_runtime_audit"]["failure_error"] == "encode crashed"
    assert details_by_name["vector_store_runtime_audit"]["memory_created"] is True
    assert details_by_name["vector_store_runtime_audit"]["success_operation"] == "add"
    assert details_by_name["vector_store_runtime_audit"]["empty_reason"] == "empty_table"
    assert details_by_name["vector_store_runtime_audit"]["empty_query_length"] == len("missing memory")
    assert details_by_name["vector_store_runtime_audit"]["failed_operation"] == "add"
    assert details_by_name["vector_store_runtime_audit"]["failed_error"] == "db down"
    assert details_by_name["vector_store_runtime_audit"]["failed_memory_id"] == ""
    assert details_by_name["vector_store_runtime_audit"]["empty_results"] == 0
    assert details_by_name["soul_runtime_audit"]["default_used"] is True
    assert details_by_name["soul_runtime_audit"]["default_contains_title"] is True
    assert details_by_name["soul_runtime_audit"]["write_length"] == len("# Soul\n\n## Identity\nHero")
    assert details_by_name["soul_runtime_audit"]["read_length"] == len("# Soul\n\n## Identity\nHero")
    assert details_by_name["soul_runtime_audit"]["ensure_created"] is False
    assert details_by_name["soul_runtime_audit"]["failed_operation"] == "write"
    assert details_by_name["soul_runtime_audit"]["failed_error"] == "denied"
    assert details_by_name["soul_runtime_audit"]["read_back_contains_hero"] is True
    assert details_by_name["filesystem_runtime_audit"]["missing_reason"] == "missing_file"
    assert details_by_name["filesystem_runtime_audit"]["write_length"] == len("hello filesystem")
    assert details_by_name["filesystem_runtime_audit"]["read_length"] == len("hello filesystem")
    assert details_by_name["filesystem_runtime_audit"]["blocked_path"] == "../../etc/passwd"
    assert details_by_name["filesystem_runtime_audit"]["blocked_error_contains_traversal"] is True
    assert details_by_name["filesystem_runtime_audit"]["not_a_file_reason"] == "not_a_file"
    assert details_by_name["filesystem_runtime_audit"]["write_failed_error"] == "denied"
    assert details_by_name["filesystem_runtime_audit"]["write_result_contains_success"] is True
    assert details_by_name["filesystem_runtime_audit"]["missing_result_contains_not_found"] is True
    assert details_by_name["filesystem_runtime_audit"]["read_result_matches"] is True
    assert details_by_name["filesystem_runtime_audit"]["not_a_file_result_contains_error"] is True
    assert details_by_name["filesystem_runtime_audit"]["write_failure_contains_error"] is True
    assert details_by_name["vault_runtime_audit"]["stored_value_matches"] is True
    assert details_by_name["vault_runtime_audit"]["missing_get_is_none"] is True
    assert details_by_name["vault_runtime_audit"]["store_action"] == "created"
    assert details_by_name["vault_runtime_audit"]["list_key_count"] == 1
    assert details_by_name["vault_runtime_audit"]["redaction_value_count"] == 1
    assert details_by_name["vault_runtime_audit"]["decryptable_count"] == 1
    assert details_by_name["vault_runtime_audit"]["undecryptable_count"] == 0
    assert details_by_name["vault_runtime_audit"]["delete_success"] is True
    assert details_by_name["vault_runtime_audit"]["delete_missing"] is False
    assert details_by_name["vault_runtime_audit"]["missing_get_reason"] == "missing_secret"
    assert details_by_name["vault_runtime_audit"]["missing_delete_reason"] == "missing_secret"
    assert details_by_name["vault_runtime_audit"]["failed_operation"] == "get"
    assert details_by_name["vault_runtime_audit"]["failed_error"] == "bad decrypt"
    assert details_by_name["runtime_model_overrides"]["completion_runtime_profile"] == "default"
    assert details_by_name["runtime_model_overrides"]["completion_model"] == "openai/gpt-4o-mini"
    assert details_by_name["runtime_model_overrides"]["agent_runtime_profile"] == "default"
    assert details_by_name["runtime_model_overrides"]["agent_model"] == "openai/gpt-4.1-mini"
    assert details_by_name["runtime_fallback_overrides"]["completion_attempted_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]
    assert details_by_name["runtime_fallback_overrides"]["completion_final_model"] == "openai/gpt-4.1-nano"
    assert details_by_name["runtime_fallback_overrides"]["agent_fallback_models"] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]
    assert details_by_name["runtime_profile_preferences"]["completion_runtime_profile"] == "local"
    assert details_by_name["runtime_profile_preferences"]["completion_attempted_models"] == [
        "ollama/llama3.2",
        "openrouter/anthropic/claude-sonnet-4",
    ]
    assert details_by_name["runtime_profile_preferences"]["completion_final_model"] == "openrouter/anthropic/claude-sonnet-4"
    assert details_by_name["runtime_profile_preferences"]["agent_runtime_profile"] == "local"
    assert details_by_name["runtime_profile_preferences"]["agent_fallback_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-mini",
    ]
    assert details_by_name["runtime_path_patterns"]["wildcard_runtime_profile"] == "local"
    assert details_by_name["runtime_path_patterns"]["wildcard_model"] == "openai/gpt-4.1-mini"
    assert details_by_name["runtime_path_patterns"]["wildcard_fallback_models"] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]
    assert details_by_name["runtime_path_patterns"]["exact_runtime_profile"] == "local"
    assert details_by_name["runtime_path_patterns"]["exact_model"] == "ollama/coder"
    assert details_by_name["runtime_path_patterns"]["exact_fallback_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]
    assert details_by_name["provider_policy_capabilities"]["chat_runtime_profile"] == "local"
    assert details_by_name["provider_policy_capabilities"]["chat_fallback_models"] == [
        "openai/gpt-4.1-mini",
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-nano",
        "openai/gpt-4o-mini",
    ]
    assert details_by_name["provider_policy_capabilities"]["completion_attempted_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
    ]
    assert details_by_name["provider_policy_capabilities"]["completion_final_model"] == "openai/gpt-4o-mini"
    assert details_by_name["provider_policy_scoring"]["completion_attempted_models"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-nano",
    ]
    assert details_by_name["provider_policy_scoring"]["completion_final_model"] == "openai/gpt-4.1-nano"
    assert details_by_name["provider_policy_scoring"]["completion_weighted_scores"] == {
        "fast": 5.0,
        "cheap": 4.0,
        "tool_use": 4.0,
    }
    assert details_by_name["provider_policy_scoring"]["agent_weighted_scores"] == {
        "fast": 6.0,
        "reasoning": 4.0,
        "tool_use": 4.0,
    }
    assert details_by_name["provider_policy_scoring"]["agent_fallback_models"] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4o-mini",
    ]
    assert details_by_name["provider_policy_safeguards"]["attempted_models"] == ["openai/gpt-4o-mini"]
    assert details_by_name["provider_policy_safeguards"]["selected_model"] == "openai/gpt-4o-mini"
    assert details_by_name["provider_policy_safeguards"]["rerouted_from_policy_guardrails"] is True
    assert details_by_name["provider_policy_safeguards"]["required_policy_intents"] == ["tool_use"]
    assert details_by_name["provider_policy_safeguards"]["max_cost_tier"] == "medium"
    assert details_by_name["provider_policy_safeguards"]["max_latency_tier"] == "medium"
    assert details_by_name["provider_policy_safeguards"]["required_task_class"] == "chat"
    assert details_by_name["provider_policy_safeguards"]["max_budget_class"] == "medium"
    assert details_by_name["provider_policy_safeguards"]["primary_missing_required_intents"] == ["tool_use"]
    assert details_by_name["provider_policy_safeguards"]["primary_cost_guardrail"] is False
    assert details_by_name["provider_policy_safeguards"]["primary_latency_guardrail"] is False
    assert details_by_name["provider_policy_safeguards"]["primary_task_class"] == "analysis"
    assert details_by_name["provider_policy_safeguards"]["primary_task_guardrail"] is False
    assert details_by_name["provider_policy_safeguards"]["primary_budget_class"] == "high"
    assert details_by_name["provider_policy_safeguards"]["primary_budget_guardrail"] is False
    assert details_by_name["provider_routing_decision_audit"]["completion_selected_model"] == (
        "openrouter/anthropic/claude-sonnet-4"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_attempt_order"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
        "openai/gpt-4.1-mini",
    ]
    assert details_by_name["provider_routing_decision_audit"]["completion_budget_steering_mode"] == "prefer_lower_budget"
    assert details_by_name["provider_routing_decision_audit"]["completion_selected_route_score"] < 0.0
    assert details_by_name["provider_routing_decision_audit"]["completion_selection_policy_mode"] == (
        "retain_primary_until_reroute"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_planning_winner_model"] == (
        "openai/gpt-4o-mini"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_planning_winner_selected"] is False
    assert details_by_name["provider_routing_decision_audit"]["completion_best_alternate_model"] == (
        "openai/gpt-4o-mini"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_selected_vs_best_alternate_margin"] < 0.0
    assert details_by_name["provider_routing_decision_audit"]["completion_selected_failure_risk_score"] == 0.0
    assert details_by_name["provider_routing_decision_audit"]["completion_selected_production_readiness"] == "ready"
    assert details_by_name["provider_routing_decision_audit"]["completion_route_explanation"].startswith(
        "selected openrouter/anthropic/claude-sonnet-4"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_route_comparison_summary"].startswith(
        "retained primary openrouter/anthropic/claude-sonnet-4 even though openai/gpt-4o-mini"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_simulated_route_count"] == 4
    assert details_by_name["provider_routing_decision_audit"]["completion_first_route_entry"] == (
        "openrouter/anthropic/claude-sonnet-4"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_rejected_summary_count"] == 3
    assert details_by_name["provider_routing_decision_audit"]["completion_rejected_models"] == [
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
        "openai/gpt-4.1-mini",
    ]
    assert details_by_name["provider_routing_decision_audit"]["agent_selected_model"] == "ollama/llama3.2"
    assert details_by_name["provider_routing_decision_audit"]["agent_attempt_order"] == [
        "ollama/llama3.2",
        "openai/gpt-4.1-nano",
        "openai/gpt-4o-mini",
    ]
    assert details_by_name["provider_routing_decision_audit"]["agent_budget_steering_mode"] == "none"
    assert details_by_name["provider_routing_decision_audit"]["agent_selection_policy_mode"] == (
        "highest_ranked_attemptable"
    )
    assert details_by_name["provider_routing_decision_audit"]["agent_planning_winner_model"] == "ollama/llama3.2"
    assert details_by_name["provider_routing_decision_audit"]["agent_planning_winner_selected"] is True
    assert details_by_name["provider_routing_decision_audit"]["agent_best_alternate_model"] == (
        "openai/gpt-4.1-nano"
    )
    assert details_by_name["provider_routing_decision_audit"]["agent_selected_vs_best_alternate_margin"] >= 0.0
    assert details_by_name["provider_routing_decision_audit"]["agent_primary_decision"] == "skipped"
    assert details_by_name["provider_routing_decision_audit"]["agent_primary_feedback_state"] == "cooldown"
    assert details_by_name["provider_routing_decision_audit"]["agent_primary_failure_risk_score"] > 0.0
    assert details_by_name["provider_routing_decision_audit"]["agent_route_comparison_summary"].startswith(
        "selected ollama/llama3.2 over openai/gpt-4.1-nano"
    )
    assert "unhealthy_cooldown" in details_by_name["provider_routing_decision_audit"][
        "agent_primary_reason_codes"
    ]
    assert details_by_name["session_bound_llm_trace"]["session_id"] == "trace-session"
    assert details_by_name["session_bound_llm_trace"]["title_trace_has_request_id"] is True
    assert details_by_name["session_bound_llm_trace"]["consolidation_trace_has_request_id"] is True
    assert details_by_name["session_bound_llm_trace"]["request_ids_differ"] is True
    assert details_by_name["session_consolidation_behavior"]["stored_memory_count"] == 2
    assert details_by_name["session_consolidation_behavior"]["soul_update_count"] == 1
    assert details_by_name["session_consolidation_behavior"]["memory_categories"] == ["fact", "goal"]
    assert details_by_name["session_consolidation_behavior"]["stored_texts"] == [
        "User is building a guardian workspace",
        "Ship behavioral guardian evals",
    ]
    assert details_by_name["session_consolidation_behavior"]["updated_soul_section"] == "Goals"
    assert details_by_name["session_consolidation_behavior"]["updated_soul_mentions_workspace"] is True
    assert details_by_name["memory_engineering_retrieval_benchmark_behavior"]["engineering_memory_has_issue_reference"] is True
    assert details_by_name["memory_engineering_retrieval_benchmark_behavior"]["engineering_memory_has_pr_reference"] is True
    assert details_by_name["memory_engineering_retrieval_benchmark_behavior"]["engineering_memory_has_approval_reference"] is True
    assert details_by_name["memory_engineering_retrieval_benchmark_behavior"]["engineering_memory_has_artifact_reference"] is True
    assert details_by_name["memory_engineering_retrieval_benchmark_behavior"]["benchmark_suite_named"] is True
    assert details_by_name["memory_engineering_retrieval_benchmark_behavior"]["benchmark_dimensions_visible"] is True
    assert details_by_name["memory_contradiction_ranking_behavior"]["keeps_current_truth"] is True
    assert details_by_name["memory_contradiction_ranking_behavior"]["suppresses_lower_ranked_contradiction"] is True
    assert details_by_name["memory_contradiction_ranking_behavior"]["suppressed_contradiction_count"] is True
    assert details_by_name["memory_contradiction_ranking_behavior"]["ranking_policy_visible"] is True
    assert details_by_name["memory_contradiction_ranking_behavior"]["suppressed_example_reports_winner"] is True
    assert details_by_name["memory_selective_forgetting_surface_behavior"]["selective_forgetting_state_active"] is True
    assert details_by_name["memory_selective_forgetting_surface_behavior"]["policy_declares_lower_ranked_contradiction"] is True
    assert details_by_name["memory_selective_forgetting_surface_behavior"]["policy_declares_stale_provider_suppression"] is True
    assert details_by_name["memory_selective_forgetting_surface_behavior"]["failure_report_has_conflict"] is True
    assert details_by_name["memory_selective_forgetting_surface_behavior"]["failure_report_has_archive"] is True
    assert details_by_name["operator_memory_benchmark_surface_behavior"]["suite_name_visible"] is True
    assert details_by_name["operator_memory_benchmark_surface_behavior"]["operator_status_visible"] is True
    assert details_by_name["operator_memory_benchmark_surface_behavior"]["scenario_count_matches"] is True
    assert details_by_name["operator_memory_benchmark_surface_behavior"]["failure_taxonomy_visible"] is True
    assert details_by_name["operator_memory_benchmark_surface_behavior"]["ci_gate_mode_visible"] is True
    assert details_by_name["memory_commitment_continuity_behavior"]["baseline_bucket_excludes_linked_commitment"] is True
    assert details_by_name["memory_commitment_continuity_behavior"]["baseline_context_excludes_linked_commitment"] is True
    assert details_by_name["memory_commitment_continuity_behavior"]["linked_project_present"] is True
    assert details_by_name["memory_commitment_continuity_behavior"]["memory_context_has_commitment"] is True
    assert details_by_name["memory_collaborator_lookup_behavior"]["baseline_bucket_excludes_linked_collaborator"] is True
    assert details_by_name["memory_collaborator_lookup_behavior"]["baseline_context_excludes_linked_collaborator"] is True
    assert details_by_name["memory_collaborator_lookup_behavior"]["collaborator_present"] is True
    assert details_by_name["memory_collaborator_lookup_behavior"]["memory_context_has_collaborator"] is True
    assert details_by_name["memory_collaborator_lookup_behavior"]["active_projects_has_atlas"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["provider_runtime_ready"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["provider_user_model_ready"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["provider_contract_authoritative_guardian"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["provider_contract_advisory_provenance"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["provider_adapter_model_user_sync_policy"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["world_model_has_provider_collaborator"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["world_model_has_provider_obligation"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["memory_context_has_provider_project"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["memory_provider_diagnostics_visible"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["memory_provider_quality_focused"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["provider_query_hint_without_recent_project"] is True
    assert details_by_name["memory_provider_user_model_behavior"]["memory_provider_diagnostics_show_authority"] is True
    assert details_by_name["memory_provider_stale_evidence_behavior"]["fresh_project_kept"] is True
    assert details_by_name["memory_provider_stale_evidence_behavior"]["stale_collaborator_suppressed"] is True
    assert details_by_name["memory_provider_stale_evidence_behavior"]["stale_hit_count"] is True
    assert details_by_name["memory_provider_stale_evidence_behavior"]["stale_collaborator_bucket"] is True
    assert details_by_name["memory_provider_stale_evidence_behavior"]["quality_state_guarded"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_consolidation_ready"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_contract_is_guarded"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_adapter_model_writeback_requires_canonical"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_called"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["audit_has_provider_writeback"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["audit_has_no_provider_failures"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["canonical_memory_kept_project"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_suppressed_low_quality"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_suppressed_missing_project_anchor"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_sync_policy_guarded"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_runtime_contract_visible"] is True
    assert details_by_name["bounded_memory_snapshot_behavior"]["bounded_snapshot_is_stable_within_session"] is True
    assert details_by_name["bounded_memory_snapshot_behavior"]["bounded_snapshot_includes_todo_overlay"] is True
    assert details_by_name["bounded_memory_snapshot_behavior"]["bounded_snapshot_line_count"] <= 8
    assert details_by_name["bounded_memory_snapshot_behavior"]["same_session_excludes_new_project"] is True
    assert details_by_name["bounded_memory_snapshot_behavior"]["new_session_sees_new_project"] is True
    assert details_by_name["bounded_memory_snapshot_behavior"]["new_session_uses_real_session_record"] is True
    assert details_by_name["memory_supersession_filter_behavior"]["active_project_present"] is True
    assert details_by_name["memory_supersession_filter_behavior"]["superseded_project_filtered"] is True
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["contradiction_count"] == 1
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["superseded_count"] == 1
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["superseded_memory_count"] == 1
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["hybrid_filters_superseded"] is True
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["hybrid_keeps_current"] is True
    assert (
        details_by_name["memory_decay_contradiction_cleanup_behavior"]["guardian_context_filters_superseded"]
        is True
    )
    assert (
        details_by_name["memory_decay_contradiction_cleanup_behavior"]["guardian_context_keeps_current"]
        is True
    )
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_state_conflict_and_forgetting"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_policy_authoritative_guardian"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_policy_selective_forgetting"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_superseded_count"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_archived_count"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_conflict_summary_visible"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reconciliation_diagnostics_visible"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reports_conflict_and_forgetting"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reports_policy_contract"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reports_recent_conflict"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reports_recent_archival"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["baseline_snapshot_has_no_procedural_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["same_session_snapshot_refreshes"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["adapted_memory_context_has_timing_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["adapted_memory_context_has_delivery_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["adapted_bounded_context_has_timing_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["active_procedural_memory_count"] == 8
    assert details_by_name["procedural_memory_adaptation_behavior"]["bounded_snapshot_line_count"] <= 8
    assert details_by_name["scheduled_local_runtime_profile"]["runtime_profile"] == "local"
    assert details_by_name["scheduled_local_runtime_profile"]["routed_models"] == {
        "daily_briefing": "ollama/llama3.2",
        "evening_review": "ollama/llama3.2",
        "activity_digest": "ollama/llama3.2",
        "weekly_activity_review": "ollama/llama3.2",
    }
    assert details_by_name["scheduled_local_runtime_profile"]["routed_api_bases"] == {
        "daily_briefing": "http://localhost:11434/v1",
        "evening_review": "http://localhost:11434/v1",
        "activity_digest": "http://localhost:11434/v1",
        "weekly_activity_review": "http://localhost:11434/v1",
    }
    assert details_by_name["scheduled_local_runtime_profile"]["delivery_count"] == 4
    assert details_by_name["process_recovery_boundary_behavior"]["session_scoped"] is True
    assert details_by_name["process_recovery_boundary_behavior"]["output_path_within_workspace"] is False
    assert details_by_name["process_recovery_boundary_behavior"]["output_path_under_runtime_tmp"] is True
    assert details_by_name["process_recovery_boundary_behavior"]["owner_list_includes_process"] is True
    assert details_by_name["process_recovery_boundary_behavior"]["owner_output_visible"] is True
    assert details_by_name["process_recovery_boundary_behavior"]["owner_stop_succeeds"] is True
    assert details_by_name["process_recovery_boundary_behavior"]["other_list_hidden"] is True
    assert details_by_name["process_recovery_boundary_behavior"]["other_read_hidden"] is True
    assert details_by_name["process_recovery_boundary_behavior"]["other_stop_hidden"] is True
    assert details_by_name["daily_briefing_delivery_behavior"]["message_type"] == "proactive"
    assert details_by_name["daily_briefing_delivery_behavior"]["intervention_type"] == "advisory"
    assert details_by_name["daily_briefing_delivery_behavior"]["scheduled_delivery"] is True
    assert details_by_name["daily_briefing_delivery_behavior"]["content_contains_design_review"] is True
    assert details_by_name["daily_briefing_delivery_behavior"]["upcoming_event_count"] == 1
    assert details_by_name["daily_briefing_delivery_behavior"]["data_quality"] == "good"
    assert details_by_name["shell_tool_runtime_audit"]["timeout_seconds"] == 35
    assert details_by_name["browser_runtime_audit"]["timeout_seconds"] == 30
    assert details_by_name["browser_runtime_audit"]["hostname"] == "example.com"
    assert details_by_name["web_search_runtime_audit"]["timeout_seconds"] == 15
    assert details_by_name["web_search_runtime_audit"]["query_length"] == len("slow search")
    assert details_by_name["web_search_empty_result_audit"]["query_length"] == len("empty query")
    assert details_by_name["web_search_empty_result_audit"]["result_count"] == 0
    assert details_by_name["observer_calendar_source_audit"]["upcoming_event_count"] == 1
    assert details_by_name["observer_git_source_audit"]["reason"] == "missing_git_dir"
    assert details_by_name["observer_goal_source_audit"]["goal_count"] == 2
    assert details_by_name["observer_goal_source_audit"]["domain_count"] == 2
    assert details_by_name["observer_time_source_audit"]["time_of_day"] == "morning"
    assert details_by_name["observer_time_source_audit"]["timezone"] == "UTC"
    assert details_by_name["observer_delivery_gate_audit"]["delivered_user_state"] == "available"
    assert details_by_name["observer_delivery_gate_audit"]["queued_user_state"] == "deep_work"
    assert details_by_name["observer_delivery_gate_audit"]["delivered_connections"] == 2
    assert details_by_name["observer_delivery_transport_audit"]["direct_failure_error"] == "all_connections_failed"
    assert details_by_name["observer_delivery_transport_audit"]["direct_failure_delivered_connections"] == 0
    assert details_by_name["observer_delivery_transport_audit"]["bundle_delivered_count"] == 1
    assert details_by_name["observer_delivery_transport_audit"]["bundle_delivered_connections"] == 2
    assert details_by_name["observer_delivery_transport_audit"]["bundle_failed_count"] == 0
    assert details_by_name["observer_delivery_transport_audit"]["bundle_failed_error"] == "all_connections_failed"
    assert details_by_name["observer_delivery_transport_audit"]["bundle_failed_queue_retained"] is True
    assert details_by_name["observer_daemon_ingest_audit"]["persisted_app"] == "VS Code"
    assert details_by_name["observer_daemon_ingest_audit"]["persist_failed_error"] == "db down"
    assert details_by_name["mcp_test_api_audit"]["auth_required_status"] == "auth_required"
    assert details_by_name["mcp_test_api_audit"]["missing_env_vars"] == ["GITHUB_TOKEN"]
    assert details_by_name["mcp_test_api_audit"]["success_status"] == "ok"
    assert details_by_name["mcp_test_api_audit"]["tool_count"] == 1
    assert details_by_name["daily_briefing_degraded_memories_audit"]["background_source"] == "relevant_memories"
    assert details_by_name["daily_briefing_degraded_memories_audit"]["background_error"] == "vector_store_search_failed"
    assert details_by_name["daily_briefing_degraded_memories_audit"]["data_quality"] == "degraded"
    assert details_by_name["daily_briefing_degraded_memories_audit"]["degraded_inputs"] == ["relevant_memories"]
    assert details_by_name["daily_briefing_degraded_memories_audit"]["delivered"] is True
    assert details_by_name["activity_digest_degraded_delivery_behavior"]["message_type"] == "proactive"
    assert details_by_name["activity_digest_degraded_delivery_behavior"]["scheduled_delivery"] is True
    assert details_by_name["activity_digest_degraded_delivery_behavior"]["content_mentions_coding"] is True
    assert details_by_name["activity_digest_degraded_delivery_behavior"]["background_source"] == "screen_summary"
    assert details_by_name["activity_digest_degraded_delivery_behavior"]["degraded_inputs"] == [
        "total_tracked_minutes",
        "switch_count",
        "by_project",
        "longest_streaks",
    ]
    assert details_by_name["activity_digest_degraded_delivery_behavior"]["data_quality"] == "degraded"
    assert details_by_name["activity_digest_degraded_summary_audit"]["background_source"] == "screen_summary"
    assert details_by_name["activity_digest_degraded_summary_audit"]["missing_fields"] == [
        "total_tracked_minutes",
        "switch_count",
        "by_project",
        "longest_streaks",
    ]
    assert details_by_name["activity_digest_degraded_summary_audit"]["data_quality"] == "degraded"
    assert details_by_name["activity_digest_degraded_summary_audit"]["degraded_inputs"] == [
        "total_tracked_minutes",
        "switch_count",
        "by_project",
        "longest_streaks",
    ]
    assert details_by_name["activity_digest_degraded_summary_audit"]["delivered"] is True
    assert details_by_name["evening_review_degraded_delivery_behavior"]["message_type"] == "proactive"
    assert details_by_name["evening_review_degraded_delivery_behavior"]["scheduled_delivery"] is True
    assert details_by_name["evening_review_degraded_delivery_behavior"]["content_mentions_tomorrow"] is True
    assert details_by_name["evening_review_degraded_delivery_behavior"]["data_quality"] == "degraded"
    assert details_by_name["evening_review_degraded_delivery_behavior"]["degraded_inputs"] == [
        "messages_today",
        "completed_goals_today",
    ]
    assert details_by_name["evening_review_degraded_delivery_behavior"]["message_count"] == 0
    assert details_by_name["evening_review_degraded_inputs_audit"]["data_quality"] == "degraded"
    assert details_by_name["evening_review_degraded_inputs_audit"]["degraded_inputs"] == [
        "messages_today",
        "completed_goals_today",
    ]
    assert details_by_name["evening_review_degraded_inputs_audit"]["message_count"] == 0
    assert details_by_name["evening_review_degraded_inputs_audit"]["completed_goal_count"] == 0
    assert details_by_name["evening_review_degraded_inputs_audit"]["delivered"] is True
    assert details_by_name["mcp_test_api_audit"]["tool_names"] == ["list_prs"]
    assert details_by_name["mcp_test_api_audit"]["used_headers"] is True
    assert details_by_name["mcp_test_api_audit"]["failure_status_code"] == 502
    assert details_by_name["mcp_test_api_audit"]["failure_status"] == "connection_failed"
    assert details_by_name["mcp_test_api_audit"]["failure_error"] == "refused"
    assert details_by_name["skills_api_audit"]["updated_status"] == "updated"
    assert details_by_name["skills_api_audit"]["updated_enabled"] is False
    assert details_by_name["skills_api_audit"]["missing_status_code"] == 404
    assert details_by_name["skills_api_audit"]["missing_status"] == "not_found"
    assert details_by_name["skills_api_audit"]["reload_count"] == 2
    assert details_by_name["skills_api_audit"]["reload_enabled_count"] == 2
    assert details_by_name["skills_api_audit"]["reload_skill_names"] == ["test-skill", "simple-skill"]
    assert details_by_name["tool_policy_guardrails_behavior"]["safe_status"] == 200
    assert details_by_name["tool_policy_guardrails_behavior"]["balanced_status"] == 200
    assert details_by_name["tool_policy_guardrails_behavior"]["full_status"] == 200
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_disabled_status"] == 200
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_status"] == 200
    assert details_by_name["tool_policy_guardrails_behavior"]["safe_hides_write_file"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["safe_hides_execute_code"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["safe_hides_run_command"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["balanced_shows_write_file"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["balanced_hides_execute_code"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["balanced_hides_run_command"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["full_shows_execute_code"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["full_shows_run_command"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["full_start_process_requires_approval"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["full_start_process_approval_behavior"] == "always"
    assert details_by_name["tool_policy_guardrails_behavior"]["full_start_process_boundaries"] == [
        "container_process_management",
        "background_execution",
        "session_process_partition",
    ]
    assert details_by_name["tool_policy_guardrails_behavior"]["full_hides_shell_execute_alias"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["write_file_accepts_secret_refs"] is False
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_disabled_hides_tool"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_shows_tool"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_requires_approval"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_accepts_secret_refs"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_secret_ref_fields"] == ["headers"]
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_credential_egress_visible"] is True
    assert details_by_name["screen_repository_runtime_audit"]["empty_daily_reason"] == "no_observations"
    assert details_by_name["screen_repository_runtime_audit"]["empty_daily_total_observations"] == 0
    assert details_by_name["screen_repository_runtime_audit"]["success_daily_total_observations"] == 1
    assert details_by_name["screen_repository_runtime_audit"]["weekly_total_observations"] == 1
    assert details_by_name["screen_repository_runtime_audit"]["weekly_active_days"] == 1
    assert details_by_name["screen_repository_runtime_audit"]["weekly_failure_error"] == "db down"
    assert details_by_name["screen_repository_runtime_audit"]["cleanup_deleted_count"] == 1
    assert details_by_name["screen_repository_runtime_audit"]["cleanup_logged_deleted_count"] == 1
    assert details_by_name["screen_repository_runtime_audit"]["cleanup_skipped_count"] == 0
    assert details_by_name["screen_repository_runtime_audit"]["cleanup_skipped_logged_deleted_count"] == 0


def test_guardian_state_synthesis_is_stable_after_vector_store_runtime_audit():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "vector_store_runtime_audit",
                "guardian_state_synthesis",
            ]
        )
    )

    assert summary.failed == 0

    details_by_name = {result.name: result.details for result in summary.results}

    assert details_by_name["guardian_state_synthesis"]["overall_confidence"] == "partial"
    assert details_by_name["guardian_state_synthesis"]["memory_confidence"] == "degraded"


def test_guardian_judgment_runtime_eval_exposes_conflict_suppression():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "guardian_judgment_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = summary.results[0].details

    assert details["overall_confidence"] == "partial"
    assert details["world_model_confidence"] == "degraded"
    assert details["focus_source"] == "observer_goal_window"
    assert details["judgment_risk_count"] >= 1
    assert details["includes_project_mismatch"] is True
    assert details["decision_action"] == "defer"
    assert details["decision_reason"] == "low_guardian_confidence"


def test_memory_runtime_eval_scenarios_expose_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "memory_decay_contradiction_cleanup_behavior",
                "memory_reconciliation_policy_behavior",
                "procedural_memory_adaptation_behavior",
                "memory_provider_stale_evidence_behavior",
                "memory_provider_writeback_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details_by_name = {result.name: result.details for result in summary.results}

    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["contradiction_count"] == 1
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["superseded_count"] == 1
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["superseded_memory_count"] == 1
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["hybrid_filters_superseded"] is True
    assert details_by_name["memory_decay_contradiction_cleanup_behavior"]["hybrid_keeps_current"] is True
    assert (
        details_by_name["memory_decay_contradiction_cleanup_behavior"]["guardian_context_filters_superseded"]
        is True
    )
    assert (
        details_by_name["memory_decay_contradiction_cleanup_behavior"]["guardian_context_keeps_current"]
        is True
    )
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_state_conflict_and_forgetting"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["inventory_policy_authoritative_guardian"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reports_policy_contract"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reports_recent_conflict"] is True
    assert details_by_name["memory_reconciliation_policy_behavior"]["state_reports_recent_archival"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["baseline_snapshot_has_no_procedural_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["same_session_snapshot_refreshes"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["adapted_memory_context_has_timing_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["adapted_memory_context_has_delivery_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["adapted_bounded_context_has_timing_rule"] is True
    assert details_by_name["procedural_memory_adaptation_behavior"]["active_procedural_memory_count"] == 8
    assert details_by_name["procedural_memory_adaptation_behavior"]["bounded_snapshot_line_count"] <= 8
    assert details_by_name["memory_provider_stale_evidence_behavior"]["fresh_project_kept"] is True
    assert details_by_name["memory_provider_stale_evidence_behavior"]["stale_collaborator_suppressed"] is True
    assert details_by_name["memory_provider_stale_evidence_behavior"]["quality_state_guarded"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_consolidation_ready"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_called"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["audit_has_provider_writeback"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["audit_has_no_provider_failures"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_suppressed_low_quality"] is True
    assert details_by_name["memory_provider_writeback_behavior"]["provider_writeback_suppressed_missing_project_anchor"] is True


def test_background_session_runtime_eval_exposes_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "background_session_handoff_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = {result.name: result.details for result in summary.results}["background_session_handoff_behavior"]
    assert details["tracked_sessions"] is True
    assert details["running_background_process_count"] is True
    assert details["sessions_with_branch_handoff"] is True
    assert details["lead_session_is_branch_thread"] is True
    assert details["lead_session_has_running_process"] is True
    assert details["lead_session_branch_handoff_available"] is True
    assert details["lead_session_branch_target_type"] is True
    assert details["lead_session_continue_message"] is True
    assert details["lead_session_artifact_visible"] is True
    assert details["lead_session_partition_visible"] is True
    assert details["lead_session_disposable_worker_visible"] is True
    assert details["lead_session_branch_partition_visible"] is True
    assert details["blocked_session_continue_message"] is True
    assert details["blocked_session_handoff_present"] is True


def test_engineering_memory_runtime_eval_exposes_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "engineering_memory_bundle_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = {result.name: result.details for result in summary.results}["engineering_memory_bundle_behavior"]
    assert details["tracked_bundles"] is True
    assert details["search_match_count"] is True
    assert details["pull_request_bundle_count"] is True
    assert details["first_bundle_is_pull_request"] is True
    assert details["first_bundle_has_workflow"] is True
    assert details["first_bundle_has_approval"] is True
    assert details["first_bundle_has_audit_receipt"] is True
    assert details["first_bundle_has_session_match"] is True
    assert details["first_bundle_artifact_visible"] is True
    assert details["second_bundle_is_repository"] is True
    assert details["second_bundle_has_session_match"] is True
    assert details["summary_totals_match_all_bundles"] is True


def test_workflow_context_condenser_runtime_eval_exposes_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "workflow_context_condenser_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = {result.name: result.details for result in summary.results}["workflow_context_condenser_behavior"]
    assert details["long_running_summary_visible"] is True
    assert details["compacted_summary_visible"] is True
    assert details["total_step_count_visible"] is True
    assert details["compacted_step_count_visible"] is True
    assert details["session_capsule_mentions_steps"] is True
    assert details["session_compaction_count_visible"] is True
    assert details["first_workflow_compacted"] is True
    assert details["first_workflow_steps_trimmed"] is True
    assert details["first_workflow_preserves_checkpoint"] is True
    assert details["first_workflow_preserves_approval"] is True
    assert details["first_workflow_recent_steps_trimmed"] is True
    assert details["second_workflow_preserves_repair"] is True
    assert details["second_workflow_boundary_receipt_visible"] is True
    assert details["second_workflow_approval_not_hallucinated"] is True


def test_workflow_operating_layer_runtime_eval_exposes_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "workflow_operating_layer_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = {result.name: result.details for result in summary.results}["workflow_operating_layer_behavior"]
    assert details["attention_sessions_visible"] is True
    assert details["repair_ready_summary_visible"] is True
    assert details["branch_ready_summary_visible"] is True
    assert details["debugger_ready_summary_visible"] is True
    assert details["stalled_summary_visible"] is True
    assert details["atlas_queue_state_visible"] is True
    assert details["atlas_queue_reason_visible"] is True
    assert details["atlas_queue_draft_visible"] is True
    assert details["atlas_attention_summary_visible"] is True
    assert details["brief_queue_state_visible"] is True
    assert details["brief_handoff_draft_visible"] is True
    assert details["brief_related_output_visible"] is True
    assert details["brief_output_history_visible"] is True
    assert details["brief_branch_reference_visible"] is True
    assert details["approval_workflow_recovery_path_visible"] is True
    assert details["approval_workflow_checkpoint_visible"] is True
    assert details["approval_workflow_history_visible"] is True
    assert details["brief_workflow_fresh_run_visible"] is True
    assert details["brief_workflow_repair_action_visible"] is True
    assert details["brief_workflow_compare_ready"] is True
    assert details["cleanup_workflow_stalled_visible"] is True


def test_operator_continuity_graph_runtime_eval_exposes_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "operator_continuity_graph_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = {result.name: result.details for result in summary.results}["operator_continuity_graph_behavior"]
    assert details["tracked_sessions"] is True
    assert details["workflow_count"] is True
    assert details["approval_count"] is True
    assert details["notification_count"] is True
    assert details["queued_insight_count"] is True
    assert details["artifact_count"] is True
    assert details["atlas_session_continue_message"] is True
    assert details["atlas_session_workflow_count"] is True
    assert details["atlas_session_artifact_count"] is True
    assert details["has_session_workflow_edge"] is True
    assert details["has_workflow_artifact_edge"] is True
    assert details["has_notification_intervention_edge"] is True
    assert details["has_queued_intervention_edge"] is True
    assert details["has_inferred_notification_intervention_edge"] is True
    assert details["inferred_intervention_marks_missing_recent_context"] is True


def test_operator_guardian_state_surface_runtime_eval_exposes_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "operator_guardian_state_surface_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = {result.name: result.details for result in summary.results}[
        "operator_guardian_state_surface_behavior"
    ]
    assert details["session_id_matches"] is True
    assert details["overall_confidence"] == "partial"
    assert details["intent_resolution"] == "clarify_first"
    assert details["action_posture"] == "clarify_first"
    assert details["focus_source"] == "observer_goal_window"
    assert details["user_model_confidence"] == "grounded"
    assert details["has_project_target_proof"] is True
    assert details["has_judgment_risk"] is True
    assert details["has_learning_diagnostic"] is True
    assert details["has_restraint_reason"] is True
    assert details["user_model_facets_visible"] is True
    assert details["user_model_restraint_posture"] == "clarify_before_personalizing"
    assert details["next_up_mentions_clarify"] is True
    assert details["observer_project"] == "Atlas"


def test_guardian_user_model_and_restraint_runtime_evals_expose_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "guardian_user_model_continuity_behavior",
                "guardian_clarification_restraint_behavior",
            ]
        )
    )

    assert summary.failed == 0

    details = {result.name: result.details for result in summary.results}

    assert details["guardian_user_model_continuity_behavior"]["confidence"] == "grounded"
    assert details["guardian_user_model_continuity_behavior"]["facet_count"] >= 3
    assert details["guardian_user_model_continuity_behavior"]["evidence_store_count"] >= 3
    assert details["guardian_user_model_continuity_behavior"]["restraint_posture"] in {
        "clarify_before_personalizing",
        "guard_async_delivery",
    }
    assert details["guardian_user_model_continuity_behavior"]["continuity_strategy"] == "prefer_existing_thread"
    assert details["guardian_user_model_continuity_behavior"]["has_clarification_watchpoint"] is True
    assert details["guardian_user_model_continuity_behavior"]["has_existing_thread_facet"] is True
    assert details["guardian_user_model_continuity_behavior"]["has_brief_literal_facet"] is True
    assert details["guardian_user_model_continuity_behavior"]["prompt_includes_user_model_profile"] is True

    assert details["guardian_clarification_restraint_behavior"]["intent_uncertainty_level"] in {"medium", "high"}
    assert details["guardian_clarification_restraint_behavior"]["intent_resolution"] in {
        "clarify",
        "proceed_with_caution",
        "defer_or_clarify",
    }
    assert details["guardian_clarification_restraint_behavior"]["action_posture"] in {
        "clarify_first",
        "guarded_action",
    }
    assert details["guardian_clarification_restraint_behavior"]["restraint_reason_count"] >= 1
    assert details["guardian_clarification_restraint_behavior"]["user_model_benchmark_diagnostic_count"] >= 1
    assert details["guardian_clarification_restraint_behavior"]["has_benchmark_state_line"] is True
    assert details["guardian_clarification_restraint_behavior"]["prompt_includes_restraint_reasons"] is True
    assert details["guardian_clarification_restraint_behavior"]["prompt_includes_user_model_benchmark"] is True
