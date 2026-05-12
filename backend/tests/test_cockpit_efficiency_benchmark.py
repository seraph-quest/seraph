from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.cockpit.efficiency_benchmark import (
    COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES,
    COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME,
    build_cockpit_efficiency_benchmark_report,
    cockpit_efficiency_failure_taxonomy,
    cockpit_efficiency_policy_payload,
    cockpit_efficiency_scorecard,
    cockpit_efficiency_scripted_tasks,
)


def test_cockpit_efficiency_scripted_tasks_define_thresholded_fixture_contract():
    tasks = cockpit_efficiency_scripted_tasks()
    task_names = {task["task"] for task in tasks}

    assert task_names == {
        "inspect",
        "approve",
        "deny",
        "pause",
        "resume",
        "retry",
        "repair",
        "branch",
        "compare",
        "revoke",
        "audit",
    }
    assert all(task["max_actions"] > 0 for task in tasks)
    assert all(task["max_seconds"] > 0 for task in tasks)
    assert all(task["initial_state"] for task in tasks)
    assert all(task["success_condition"] for task in tasks)
    assert all(len(task["measured_counters"]) >= 3 for task in tasks)
    assert len({task["receipt"] for task in tasks}) == len(tasks)


def test_cockpit_efficiency_policy_and_scorecard_bound_claims_to_fixture_proxy():
    policy = cockpit_efficiency_policy_payload()
    scorecard = cockpit_efficiency_scorecard()
    taxonomy_names = {item["name"] for item in cockpit_efficiency_failure_taxonomy()}

    assert policy["benchmark_suite"] == COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME
    assert policy["baseline_policy"] == "baseline_is_current_seraph_fixture_not_competitor_superiority_claim"
    assert (
        policy["claim_boundary"]
        == "deterministic_operator_efficiency_fixture_not_live_multi_operator_usability_study"
    )
    assert scorecard["max_actions_total"] == 33
    assert scorecard["max_seconds_total"] == 195
    assert scorecard["confidence_measurement_boundary"] == "confidence_affordance_proxy_not_operator_reported_confidence"
    assert "unsupported_superiority_claim" in taxonomy_names


@pytest.mark.asyncio
async def test_cockpit_efficiency_benchmark_report_summarizes_successful_run():
    summary = SimpleNamespace(
        total=len(COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES),
        passed=len(COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES),
        failed=0,
        duration_ms=25,
        results=[
            SimpleNamespace(name=name, passed=True, error="")
            for name in COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES
        ],
    )

    with patch(
        "src.cockpit.efficiency_benchmark._run_cockpit_efficiency_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        payload = await build_cockpit_efficiency_benchmark_report()

    assert payload["summary"]["benchmark_posture"] == "cockpit_efficiency_ci_gated_operator_visible"
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["summary"]["scripted_task_state"] == "inspect_to_audit_paths_measured"
    assert payload["scorecard"]["task_count"] == 11
    assert payload["failure_report"] == []


@pytest.mark.asyncio
async def test_cockpit_efficiency_benchmark_report_surfaces_failures_without_overclaiming():
    summary = SimpleNamespace(
        total=len(COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES),
        passed=len(COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES) - 1,
        failed=1,
        duration_ms=25,
        results=[
            SimpleNamespace(
                name="cockpit_efficiency_threshold_behavior",
                passed=False,
                error="action budget exceeded",
            )
        ],
    )

    with patch(
        "src.cockpit.efficiency_benchmark._run_cockpit_efficiency_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        payload = await build_cockpit_efficiency_benchmark_report()

    assert payload["summary"]["benchmark_posture"] == "cockpit_efficiency_ci_regressions_detected_operator_visible"
    assert payload["summary"]["threshold_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "cockpit_efficiency_threshold_behavior"
    assert (
        payload["policy"]["competitor_claim_policy"]
        == "competitor_informed_expectations_require_source_dated_evidence_before_public_claims"
    )
