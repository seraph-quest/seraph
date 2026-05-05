from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.memory.superiority_benchmark import (
    M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES,
    build_m6_memory_superiority_benchmark_report,
)


@pytest.mark.asyncio
async def test_m6_memory_superiority_benchmark_report_stays_ci_gated_when_suite_passes():
    summary = SimpleNamespace(
        total=len(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES),
        passed=len(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES),
        failed=0,
        duration_ms=88,
        results=[
            SimpleNamespace(
                name="m6_long_horizon_recall_behavior",
                passed=True,
                error=None,
            )
        ],
    )

    with patch(
        "src.memory.superiority_benchmark._run_m6_memory_superiority_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_m6_memory_superiority_benchmark_report()

    assert report["summary"]["suite_name"] == "m6_memory_superiority"
    assert report["summary"]["benchmark_posture"] == "m6_ci_gated_operator_visible"
    assert report["summary"]["operator_status"] == "m6_memory_superiority_receipts_visible"
    assert report["summary"]["scenario_count"] == len(report["scenario_names"])
    assert report["summary"]["source_trust_privacy_state"] == "guardian_authority_external_advisory_no_secret_receipts"
    assert report["failure_report"] == []
    assert report["policy"]["milestone_contract"] == "m6_memory_superiority_ships_as_one_ready_pr"


@pytest.mark.asyncio
async def test_m6_memory_superiority_benchmark_report_reflects_suite_failures():
    summary = SimpleNamespace(
        total=len(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES),
        passed=len(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES) - 1,
        failed=1,
        duration_ms=104,
        results=[
            SimpleNamespace(
                name="m6_source_trust_privacy_boundary_behavior",
                passed=False,
                error="provider config leaked into operator receipt",
            )
        ],
    )

    with patch(
        "src.memory.superiority_benchmark._run_m6_memory_superiority_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_m6_memory_superiority_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "m6_regressions_detected_operator_visible"
    assert report["summary"]["active_failure_count"] == 1
    assert report["failure_report"][0]["type"] == "benchmark_regression"
    assert report["failure_report"][0]["scenario_name"] == "m6_source_trust_privacy_boundary_behavior"
