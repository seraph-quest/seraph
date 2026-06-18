from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.memory.benchmark import build_guardian_memory_benchmark_report


@pytest.mark.asyncio
async def test_guardian_memory_benchmark_report_reflects_suite_failures():
    summary = SimpleNamespace(
        total=8,
        passed=7,
        failed=1,
        duration_ms=140,
        results=[
            SimpleNamespace(
                name="memory_engineering_retrieval_benchmark_behavior",
                passed=False,
                error="engineering continuity retrieval missed approval evidence",
            )
        ],
    )
    reconciliation = {
        "state": "steady",
        "archived_count": 0,
        "superseded_count": 0,
        "recent_conflicts": [],
        "recent_archivals": [],
    }

    with (
        patch(
            "src.memory.benchmark._run_guardian_memory_benchmark_suite",
            AsyncMock(return_value=summary),
        ),
        patch(
            "src.memory.benchmark.summarize_memory_reconciliation_state",
            AsyncMock(return_value=reconciliation),
        ),
    ):
        report = await build_guardian_memory_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "ci_regressions_detected_operator_visible"
    assert report["summary"]["active_failure_count"] == 1
    assert report["latest_run"]["failed"] == 1
    assert report["failure_report"][0]["type"] == "benchmark_regression"
    assert report["failure_report"][0]["scenario_name"] == "memory_engineering_retrieval_benchmark_behavior"


@pytest.mark.asyncio
async def test_guardian_memory_benchmark_report_stays_ci_gated_when_suite_passes():
    summary = SimpleNamespace(
        total=8,
        passed=8,
        failed=0,
        duration_ms=92,
        results=[
            SimpleNamespace(
                name="memory_engineering_retrieval_benchmark_behavior",
                passed=True,
                error=None,
            )
        ],
    )
    reconciliation = {
        "state": "steady",
        "archived_count": 0,
        "superseded_count": 0,
        "recent_conflicts": [],
        "recent_archivals": [],
    }

    with (
        patch(
            "src.memory.benchmark._run_guardian_memory_benchmark_suite",
            AsyncMock(return_value=summary),
        ),
        patch(
            "src.memory.benchmark.summarize_memory_reconciliation_state",
            AsyncMock(return_value=reconciliation),
        ),
    ):
        report = await build_guardian_memory_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["failure_report"] == []
    assert report["latest_run"]["failed"] == 0


@pytest.mark.asyncio
async def test_guardian_memory_benchmark_report_marks_embedded_mode_as_not_run():
    reconciliation = {
        "state": "steady",
        "archived_count": 0,
        "superseded_count": 0,
        "recent_conflicts": [],
        "recent_archivals": [],
    }

    with patch(
        "src.memory.benchmark.summarize_memory_reconciliation_state",
        AsyncMock(return_value=reconciliation),
    ):
        report = await build_guardian_memory_benchmark_report(run_suite=False)

    assert report["summary"]["benchmark_posture"] == "suite_contract_visible_not_run"
    assert report["summary"]["active_failure_count"] == 0
    assert report["latest_run"]["executed"] is False
    assert report["latest_run"]["total"] is None
