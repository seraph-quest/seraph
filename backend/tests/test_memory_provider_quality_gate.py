from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.memory.provider_quality_gate import (
    MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES,
    build_memory_provider_quality_gate_report,
    memory_provider_quality_gate_failure_taxonomy,
)
from src.memory.providers import memory_provider_quality_gate_policy_payload


def test_memory_provider_quality_gate_policy_requires_pre_context_declarations():
    policy = memory_provider_quality_gate_policy_payload()
    failure_names = {item["name"] for item in memory_provider_quality_gate_failure_taxonomy()}

    assert "provenance" in policy["required_declarations"]
    assert "confidence" in policy["required_declarations"]
    assert "privacy_boundary" in policy["required_declarations"]
    assert "freshness_or_created_at" in policy["required_declarations"]
    assert "evidence_id" in policy["required_declarations"]
    assert policy["minimum_context_confidence"] == 0.5
    assert "private" in policy["privacy_boundaries_suppressed_before_context"]
    assert "provider_authority_drift" in failure_names


@pytest.mark.asyncio
async def test_memory_provider_quality_gate_report_stays_ci_gated_when_suite_passes():
    summary = SimpleNamespace(
        total=len(MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES),
        passed=len(MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES),
        failed=0,
        duration_ms=80,
        results=[SimpleNamespace(name=name, passed=True, error=None) for name in MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES],
    )

    with patch(
        "src.memory.provider_quality_gate._run_memory_provider_quality_gate_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_memory_provider_quality_gate_report()

    assert report["summary"]["benchmark_posture"] == "memory_provider_quality_gate_ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["operator_control_state"] == "inspect_correct_pin_forget_and_audit_surfaces_visible"
    assert report["failure_report"] == []


@pytest.mark.asyncio
async def test_memory_provider_quality_gate_report_reflects_suite_failures():
    summary = SimpleNamespace(
        total=len(MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES),
        passed=len(MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES) - 1,
        failed=1,
        duration_ms=80,
        results=[
            SimpleNamespace(
                name="memory_provider_quality_gate_suppression_behavior",
                passed=False,
                error="noisy provider evidence reached context",
            )
        ],
    )

    with patch(
        "src.memory.provider_quality_gate._run_memory_provider_quality_gate_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_memory_provider_quality_gate_report()

    assert report["summary"]["benchmark_posture"] == "memory_provider_quality_gate_regressions_detected_operator_visible"
    assert report["summary"]["quality_state"] == "regressions_detected"
    assert report["failure_report"][0]["scenario_name"] == "memory_provider_quality_gate_suppression_behavior"
