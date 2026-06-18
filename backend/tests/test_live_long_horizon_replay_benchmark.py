from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.replay.benchmark import (
    LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES,
    LIVE_REPLAY_BENCHMARK_SUITE_NAME,
    build_live_replay_benchmark_report,
    live_replay_failure_taxonomy,
    live_replay_fixture_bundle,
    live_replay_policy_payload,
)


def test_live_replay_fixture_bundle_is_time_stable_and_cross_surface():
    fixtures = live_replay_fixture_bundle()

    assert {fixture["surface"] for fixture in fixtures} == {"memory", "workflow", "reach", "security", "cockpit"}
    assert all(fixture["time_anchor"] == "2026-03-18T09:00:00+00:00" for fixture in fixtures)
    assert all(fixture["fake_provider"].endswith("_fixture") for fixture in fixtures)
    assert all(fixture["deterministic"] is True for fixture in fixtures)
    assert all(fixture["operator_visible"] is True for fixture in fixtures)


def test_live_replay_policy_and_taxonomy_preserve_claim_boundary():
    policy = live_replay_policy_payload()
    taxonomy = live_replay_failure_taxonomy()

    assert policy["benchmark_suite"] == LIVE_REPLAY_BENCHMARK_SUITE_NAME
    assert policy["claim_boundary"] == "deterministic_liveish_replay_proof_not_live_human_outcome_or_provider_attestation"
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]
    assert "/api/operator/live-long-horizon-replay-benchmark" in policy["receipt_surfaces"]
    assert {item["surface"] for item in taxonomy} >= {"memory", "workflow", "reach", "security", "cockpit", "provider"}
    assert all({"name", "surface", "severity", "summary"}.issubset(item) for item in taxonomy)


@pytest.mark.asyncio
async def test_live_replay_benchmark_report_summarizes_success_and_failures():
    passing_summary = SimpleNamespace(
        total=len(LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES),
        passed=len(LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES),
        failed=0,
        duration_ms=10,
        results=[SimpleNamespace(name=name, passed=True, error="") for name in LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES],
    )

    with patch("src.replay.benchmark._run_live_replay_benchmark_suite", AsyncMock(return_value=passing_summary)):
        payload = await build_live_replay_benchmark_report()

    assert payload["summary"]["benchmark_posture"] == "live_replay_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == len(LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES)
    assert payload["latest_run"]["failed"] == 0
    assert payload["failure_report"] == []

    failing_summary = SimpleNamespace(
        total=len(LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES),
        passed=len(LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES) - 1,
        failed=1,
        duration_ms=10,
        results=[SimpleNamespace(name="live_replay_operator_receipt_behavior", passed=False, error="receipt gap")],
    )
    with patch("src.replay.benchmark._run_live_replay_benchmark_suite", AsyncMock(return_value=failing_summary)):
        payload = await build_live_replay_benchmark_report()

    assert payload["summary"]["benchmark_posture"] == "live_replay_ci_regressions_detected_operator_visible"
    assert payload["summary"]["operator_receipt_state"] == "regressions_detected"
    assert payload["failure_report"][0]["scenario_name"] == "live_replay_operator_receipt_behavior"
