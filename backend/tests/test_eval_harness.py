"""Tests for the repeatable runtime evaluation harness."""

import asyncio
import json

import pytest

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
    assert "provider_fallback_chain" in captured.out
    assert "provider_health_reroute" in captured.out
    assert "local_runtime_profile" in captured.out
    assert "helper_local_runtime_paths" in captured.out
    assert "context_window_summary_audit" in captured.out
    assert "agent_local_runtime_profile" in captured.out
    assert "delegation_local_runtime_profile" in captured.out
    assert "mcp_specialist_local_runtime_profile" in captured.out
    assert "embedding_runtime_audit" in captured.out
    assert "runtime_model_overrides" in captured.out
    assert "runtime_fallback_overrides" in captured.out
    assert "scheduled_local_runtime_profile" in captured.out
    assert "shell_tool_runtime_audit" in captured.out
    assert "browser_runtime_audit" in captured.out
    assert "web_search_runtime_audit" in captured.out
    assert "web_search_empty_result_audit" in captured.out
    assert "observer_calendar_source_audit" in captured.out
    assert "observer_git_source_audit" in captured.out
    assert "observer_goal_source_audit" in captured.out
    assert "observer_time_source_audit" in captured.out
    assert "observer_daemon_ingest_audit" in captured.out


def test_main_emits_json_summary(capsys):
    exit_code = main(["--scenario", "shell_tool_timeout_contract", "--indent", "0"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["failed"] == 0
    assert payload["results"][0]["name"] == "shell_tool_timeout_contract"


def test_runtime_eval_scenarios_expose_expected_details():
    summary = asyncio.run(
        run_runtime_evals(
            [
                "provider_fallback_chain",
                "provider_health_reroute",
                "local_runtime_profile",
                "helper_local_runtime_paths",
                "context_window_summary_audit",
                "agent_local_runtime_profile",
                "delegation_local_runtime_profile",
                "mcp_specialist_local_runtime_profile",
                "embedding_runtime_audit",
                "runtime_model_overrides",
                "runtime_fallback_overrides",
                "scheduled_local_runtime_profile",
                "shell_tool_runtime_audit",
                "browser_runtime_audit",
                "web_search_runtime_audit",
                "web_search_empty_result_audit",
                "observer_calendar_source_audit",
                "observer_git_source_audit",
                "observer_goal_source_audit",
                "observer_time_source_audit",
                "observer_delivery_gate_audit",
                "observer_daemon_ingest_audit",
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
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["chat_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["memory_keeper"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["orchestrator_agent"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["goal_planner"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["web_researcher"] == "ollama/llama3.2"
    assert details_by_name["delegation_local_runtime_profile"]["routed_models"]["file_worker"] == "ollama/llama3.2"
    assert details_by_name["mcp_specialist_local_runtime_profile"]["runtime_path"] == "mcp_github_actions"
    assert details_by_name["mcp_specialist_local_runtime_profile"]["routed_model"] == "ollama/llama3.2"
    assert details_by_name["embedding_runtime_audit"]["loaded_integration_type"] == "embedding_model"
    assert details_by_name["embedding_runtime_audit"]["loaded_model"] == "all-MiniLM-L6-v2"
    assert details_by_name["embedding_runtime_audit"]["vector_length"] == 2
    assert details_by_name["embedding_runtime_audit"]["failure_stage"] == "encode"
    assert details_by_name["embedding_runtime_audit"]["failure_error"] == "encode crashed"
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
    assert details_by_name["scheduled_local_runtime_profile"]["job_name"] == "daily_briefing"
    assert details_by_name["scheduled_local_runtime_profile"]["routed_model"] == "ollama/llama3.2"
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
    assert details_by_name["observer_daemon_ingest_audit"]["persisted_app"] == "VS Code"
    assert details_by_name["observer_daemon_ingest_audit"]["persist_failed_error"] == "db down"
