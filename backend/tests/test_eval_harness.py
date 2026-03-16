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
    assert "agent_local_runtime_profile" in captured.out
    assert "scheduled_local_runtime_profile" in captured.out
    assert "shell_tool_runtime_audit" in captured.out
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
                "agent_local_runtime_profile",
                "scheduled_local_runtime_profile",
                "shell_tool_runtime_audit",
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
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["chat_agent"] == "ollama/llama3.2"
    assert details_by_name["agent_local_runtime_profile"]["routed_models"]["memory_keeper"] == "ollama/llama3.2"
    assert details_by_name["scheduled_local_runtime_profile"]["job_name"] == "daily_briefing"
    assert details_by_name["scheduled_local_runtime_profile"]["routed_model"] == "ollama/llama3.2"
    assert details_by_name["shell_tool_runtime_audit"]["timeout_seconds"] == 35
    assert details_by_name["observer_delivery_gate_audit"]["delivered_user_state"] == "available"
    assert details_by_name["observer_delivery_gate_audit"]["queued_user_state"] == "deep_work"
    assert details_by_name["observer_daemon_ingest_audit"]["persisted_app"] == "VS Code"
    assert details_by_name["observer_daemon_ingest_audit"]["persist_failed_error"] == "db down"
