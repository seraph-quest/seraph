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
    summary = asyncio.run(run_runtime_evals(["daily_briefing_fallback", "strategist_tick_tool_audit"]))

    assert summary.total == 2
    assert summary.failed == 0
    assert [result.name for result in summary.results] == [
        "daily_briefing_fallback",
        "strategist_tick_tool_audit",
    ]


def test_run_runtime_evals_rejects_unknown_scenarios():
    with pytest.raises(ValueError, match="Unknown eval scenario"):
        asyncio.run(run_runtime_evals(["missing-scenario"]))


def test_main_lists_available_scenarios(capsys):
    exit_code = main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "chat_model_wrapper" in captured.out
    assert "session_title_generation_background_audit" in captured.out


def test_main_emits_json_summary(capsys):
    exit_code = main(["--scenario", "shell_tool_timeout_contract", "--indent", "0"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["failed"] == 0
    assert payload["results"][0]["name"] == "shell_tool_timeout_contract"
