from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.memory.benchmark import build_guardian_memory_benchmark_report
from src.memory import gate_a_baseline as gate_a_baseline_module
from src.memory.gate_a_baseline import (
    GATE_A_BASELINE_CLAIM_BOUNDARY,
    GATE_A_BASELINE_CORPUS_SHA256,
    GATE_A_BASELINE_METRIC_SHA256,
    GATE_A_BASELINE_METRIC_VERSION,
    GateAMeasurementReceipt,
    build_gate_a_baseline_receipt,
    gate_a_baseline_corpus,
    gate_a_baseline_corpus_sha256,
    gate_a_baseline_metric_sha256,
)


def _valid_measurement_receipt() -> GateAMeasurementReceipt:
    return GateAMeasurementReceipt(
        corpus_sha256=GATE_A_BASELINE_CORPUS_SHA256,
        metric_contract_sha256=GATE_A_BASELINE_METRIC_SHA256,
        runner_id="gate-a-runner-v1",
        execution_receipt_id="exec-gate-a-001",
    )


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


def test_gate_a_baseline_is_frozen_and_content_free():
    receipt = build_gate_a_baseline_receipt()

    assert receipt["summary"]["status"] == "degraded"
    assert receipt["summary"]["artifact_status"] == "pass"
    assert receipt["summary"]["measurement_status"] == "blocked"
    assert receipt["summary"]["claim_boundary"] == GATE_A_BASELINE_CLAIM_BOUNDARY
    assert receipt["artifact"]["metric_contract_version"] == GATE_A_BASELINE_METRIC_VERSION
    assert receipt["artifact"]["corpus_sha256"] == GATE_A_BASELINE_CORPUS_SHA256
    assert receipt["artifact"]["metric_contract_sha256"] == GATE_A_BASELINE_METRIC_SHA256
    assert receipt["artifact"]["case_count"] == len(gate_a_baseline_corpus())
    assert "runtime_measurement_not_supplied" in receipt["blocked_reasons"]
    assert receipt["safe_receipt"]["contains_memory_content"] is False
    assert receipt["safe_receipt"]["contains_secret"] is False
    assert receipt["safe_receipt"]["contains_private_path"] is False
    assert gate_a_baseline_corpus_sha256() == GATE_A_BASELINE_CORPUS_SHA256
    assert gate_a_baseline_metric_sha256() == GATE_A_BASELINE_METRIC_SHA256

    encoded = str(receipt)
    assert "sk-" not in encoded
    assert "/home/" not in encoded
    assert "OPENROUTER_API_KEY" not in encoded


def test_gate_a_baseline_measurement_has_explicit_pass_and_degraded_states():
    metric_names = [item["name"] for item in build_gate_a_baseline_receipt()["metrics"]]

    passing = build_gate_a_baseline_receipt(
        observed_metrics={name: 1.0 for name in metric_names},
        measurement_receipt=_valid_measurement_receipt(),
    )
    degraded = build_gate_a_baseline_receipt(
        observed_metrics={name: (0.0 if name == "delete_exclusion" else 1.0) for name in metric_names},
        measurement_receipt=_valid_measurement_receipt(),
    )

    assert passing["summary"]["status"] == "pass"
    assert passing["summary"]["measurement_status"] == "pass"
    assert passing["summary"]["measurement_binding_status"] == "verified"
    assert passing["summary"]["operator_status"] == "gate_a_baseline_passed"
    assert degraded["summary"]["status"] == "degraded"
    assert degraded["summary"]["measurement_status"] == "degraded"
    assert degraded["summary"]["measurement_binding_status"] == "verified"
    assert degraded["summary"]["operator_status"] == "gate_a_baseline_measurement_degraded"
    assert next(item for item in degraded["metrics"] if item["name"] == "delete_exclusion")["observed_status"] == "degraded"
    assert passing["measurement_receipt"]["runner_id"] == "gate-a-runner-v1"


def test_gate_a_baseline_requires_a_typed_measurement_binding():
    metric_names = [item["name"] for item in build_gate_a_baseline_receipt()["metrics"]]
    unbound = build_gate_a_baseline_receipt(
        observed_metrics={name: 1.0 for name in metric_names},
    )

    assert unbound["summary"]["status"] == "blocked"
    assert unbound["summary"]["measurement_status"] == "blocked"
    assert unbound["summary"]["measurement_binding_status"] == "blocked"
    assert "measurement_receipt_required" in unbound["blocked_reasons"]
    assert unbound["measurement_receipt"] is None
    assert all(item["observed_status"] == "blocked" for item in unbound["metrics"])
    assert all(item["observed_value"] is None for item in unbound["metrics"])

    invalid = build_gate_a_baseline_receipt(
        observed_metrics={name: 1.0 for name in metric_names},
        measurement_receipt=GateAMeasurementReceipt(
            corpus_sha256="0" * 64,
            metric_contract_sha256=GATE_A_BASELINE_METRIC_SHA256,
            runner_id="gate-a-runner-v1",
            execution_receipt_id="exec-gate-a-001",
        ),
    )
    assert invalid["summary"]["status"] == "blocked"
    assert "measurement_corpus_hash_mismatch" in invalid["blocked_reasons"]


def test_gate_a_baseline_blocks_measurement_when_frozen_corpus_drifts():
    metric_names = [item["name"] for item in build_gate_a_baseline_receipt()["metrics"]]
    with patch.object(
        gate_a_baseline_module,
        "_GATE_A_BASELINE_CASES",
        gate_a_baseline_module._GATE_A_BASELINE_CASES[:-1],
    ):
        receipt = build_gate_a_baseline_receipt(
            observed_metrics={name: 1.0 for name in metric_names},
            measurement_receipt=_valid_measurement_receipt(),
        )

    assert receipt["summary"]["status"] == "blocked"
    assert receipt["summary"]["artifact_status"] == "blocked"
    assert receipt["summary"]["measurement_status"] == "blocked"
    assert receipt["summary"]["measurement_binding_status"] == "blocked"
    assert "frozen_corpus_hash_mismatch" in receipt["blocked_reasons"]
    assert all(item["observed_status"] == "blocked" for item in receipt["metrics"])
    assert all(item["observed_value"] is None for item in receipt["metrics"])


def test_gate_a_baseline_blocks_incomplete_or_unknown_measurement():
    receipt = build_gate_a_baseline_receipt(
        observed_metrics={"exact_evidence_recall": 1.0, "unexpected_metric": 1.0}
    )

    assert receipt["summary"]["status"] == "blocked"
    assert receipt["summary"]["measurement_status"] == "blocked"
    assert "unknown_observed_metric:unexpected_metric" in receipt["blocked_reasons"]
    assert any(
        reason.startswith("missing_or_non_numeric_observed_metric:")
        for reason in receipt["blocked_reasons"]
    )

    malformed = build_gate_a_baseline_receipt(observed_metrics=[("exact_evidence_recall", 1.0)])
    assert malformed["summary"]["status"] == "blocked"
    assert malformed["blocked_reasons"] == ["observed_metrics_not_mapping"]


@pytest.mark.asyncio
async def test_guardian_memory_benchmark_report_exposes_gate_a_baseline_receipt():
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

    assert report["gate_a_baseline"]["summary"]["status"] == "degraded"
    assert report["gate_a_baseline"]["artifact"]["corpus_sha256"] == GATE_A_BASELINE_CORPUS_SHA256
    assert report["policy"]["gate_a_baseline_policy"].startswith("frozen_")
