"""Tests for the repeatable runtime evaluation harness."""

import asyncio
import json
from unittest.mock import patch

import pytest

from config.settings import settings
from src.evals import harness
from src.evals.harness import available_scenarios, main, run_runtime_evals


def test_run_runtime_evals_passes_all_scenarios():
    summary = asyncio.run(run_runtime_evals())

    scenario_names = {scenario.name for scenario in available_scenarios()}
    result_names = {result.name for result in summary.results}

    assert summary.total == len(scenario_names)
    assert summary.failed == 0
    assert result_names == scenario_names


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
    assert "guardian_state_synthesis" in captured.out
    assert "observer_refresh_behavior" in captured.out
    assert "observer_delivery_decision_behavior" in captured.out
    assert "native_presence_notification_behavior" in captured.out
    assert "intervention_policy_behavior" in captured.out
    assert "guardian_feedback_loop" in captured.out
    assert "provider_fallback_chain" in captured.out
    assert "provider_health_reroute" in captured.out
    assert "local_runtime_profile" in captured.out
    assert "helper_local_runtime_paths" in captured.out
    assert "context_window_summary_audit" in captured.out
    assert "agent_local_runtime_profile" in captured.out
    assert "delegation_local_runtime_profile" in captured.out
    assert "delegated_tool_workflow_behavior" in captured.out
    assert "delegated_tool_workflow_degraded_behavior" in captured.out
    assert "workflow_composition_behavior" in captured.out
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
    assert "provider_routing_decision_audit" in captured.out
    assert "session_bound_llm_trace" in captured.out
    assert "session_consolidation_behavior" in captured.out
    assert "scheduled_local_runtime_profile" in captured.out
    assert "daily_briefing_delivery_behavior" in captured.out
    assert "shell_tool_runtime_audit" in captured.out
    assert "browser_runtime_audit" in captured.out
    assert "web_search_runtime_audit" in captured.out
    assert "web_search_empty_result_audit" in captured.out
    assert "observer_calendar_source_audit" in captured.out
    assert "observer_git_source_audit" in captured.out
    assert "observer_goal_source_audit" in captured.out
    assert "observer_time_source_audit" in captured.out
    assert "observer_delivery_gate_audit" in captured.out
    assert "observer_delivery_transport_audit" in captured.out
    assert "observer_daemon_ingest_audit" in captured.out
    assert "mcp_test_api_audit" in captured.out
    assert "skills_api_audit" in captured.out
    assert "tool_policy_guardrails_behavior" in captured.out
    assert "screen_repository_runtime_audit" in captured.out
    assert "daily_briefing_degraded_memories_audit" in captured.out
    assert "activity_digest_degraded_delivery_behavior" in captured.out
    assert "activity_digest_degraded_summary_audit" in captured.out
    assert "evening_review_degraded_delivery_behavior" in captured.out
    assert "evening_review_degraded_inputs_audit" in captured.out


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
                "guardian_state_synthesis",
                "observer_refresh_behavior",
                "observer_delivery_decision_behavior",
                "native_presence_notification_behavior",
                "intervention_policy_behavior",
                "guardian_feedback_loop",
                "agent_local_runtime_profile",
                "delegation_local_runtime_profile",
                "delegated_tool_workflow_behavior",
                "delegated_tool_workflow_degraded_behavior",
                "workflow_composition_behavior",
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
                "provider_routing_decision_audit",
                "session_bound_llm_trace",
                "session_consolidation_behavior",
                "scheduled_local_runtime_profile",
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
    assert details_by_name["guardian_state_synthesis"]["overall_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["observer_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["observer_salience"] == "high"
    assert details_by_name["guardian_state_synthesis"]["observer_interruption_cost"] == "low"
    assert details_by_name["guardian_state_synthesis"]["memory_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["current_session_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["recent_sessions_confidence"] == "grounded"
    assert details_by_name["guardian_state_synthesis"]["goal_summary"] == "Ship guardian state"
    assert details_by_name["guardian_state_synthesis"]["recent_sessions_contains_title"] is True
    assert details_by_name["guardian_state_synthesis"]["current_history_mentions_guardian_state"] is True
    assert details_by_name["guardian_state_synthesis"]["instructions_include_guardian_state"] is True
    assert details_by_name["native_presence_notification_behavior"]["action"] == "act"
    assert details_by_name["native_presence_notification_behavior"]["delivery_decision"] == "deliver"
    assert details_by_name["native_presence_notification_behavior"]["transport"] == "native_notification"
    assert details_by_name["native_presence_notification_behavior"]["notification_title"] == "Seraph alert"
    assert details_by_name["native_presence_notification_behavior"]["notification_body_matches"] is True
    assert details_by_name["native_presence_notification_behavior"]["acked"] is True
    assert details_by_name["native_presence_notification_behavior"]["remaining_notifications"] == 0
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
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["chat_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["onboarding_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["strategist_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["memory_keeper"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["orchestrator_agent"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["goal_planner"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["web_researcher"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["file_worker"] == "ollama/llama3.2"
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
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-mini",
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
    assert details_by_name["provider_routing_decision_audit"]["completion_selected_model"] == (
        "openrouter/anthropic/claude-sonnet-4"
    )
    assert details_by_name["provider_routing_decision_audit"]["completion_attempt_order"] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
        "openai/gpt-4.1-mini",
    ]
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
    assert details_by_name["provider_routing_decision_audit"]["agent_primary_decision"] == "skipped"
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
        "User is building a guardian cockpit",
        "Ship behavioral guardian evals",
    ]
    assert details_by_name["session_consolidation_behavior"]["updated_soul_section"] == "Goals"
    assert details_by_name["session_consolidation_behavior"]["updated_soul_mentions_cockpit"] is True
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
    assert details_by_name["tool_policy_guardrails_behavior"]["safe_hides_shell_execute"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["balanced_shows_write_file"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["balanced_hides_shell_execute"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["full_shows_shell_execute"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_disabled_hides_tool"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_shows_tool"] is True
    assert details_by_name["tool_policy_guardrails_behavior"]["mcp_approval_requires_approval"] is True
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
