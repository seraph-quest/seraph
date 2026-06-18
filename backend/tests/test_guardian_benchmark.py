from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.guardian.benchmark import build_guardian_user_model_benchmark_report


@pytest.mark.asyncio
async def test_guardian_user_model_benchmark_report_reflects_suite_failures():
    summary = SimpleNamespace(
        total=4,
        passed=3,
        failed=1,
        duration_ms=120,
        results=[
            SimpleNamespace(
                name="guardian_clarification_restraint_behavior",
                passed=False,
                error="clarification receipt missing",
            )
        ],
    )

    with patch(
        "src.guardian.benchmark._run_guardian_user_model_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_guardian_user_model_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "ci_regressions_detected_operator_visible"
    assert report["summary"]["active_failure_count"] == 1
    assert report["latest_run"]["failed"] == 1
    assert report["failure_report"][0]["scenario_name"] == "guardian_clarification_restraint_behavior"
    assert report["failure_report"][0]["type"] == "benchmark_regression"


@pytest.mark.asyncio
async def test_guardian_user_model_benchmark_report_stays_ci_gated_when_suite_passes():
    summary = SimpleNamespace(
        total=4,
        passed=4,
        failed=0,
        duration_ms=95,
        results=[
            SimpleNamespace(
                name="guardian_user_model_continuity_behavior",
                passed=True,
                error=None,
            )
        ],
    )

    with patch(
        "src.guardian.benchmark._run_guardian_user_model_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_guardian_user_model_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["failure_report"] == []
