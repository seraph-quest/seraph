from __future__ import annotations

from typing import Any


LIVE_REPLAY_BENCHMARK_SUITE_NAME = "live_long_horizon_eval_replay_v1"
LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES = (
    "live_replay_fixture_contract_behavior",
    "live_replay_cross_surface_failure_taxonomy_behavior",
    "live_replay_surface_coverage_behavior",
    "live_replay_operator_receipt_behavior",
    "operator_live_replay_benchmark_surface_behavior",
)


_TIME_ANCHOR = "2026-03-18T09:00:00+00:00"


def live_replay_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "time_stable_fixtures",
            "label": "Time-stable replay fixtures",
            "summary": "Long-horizon scenarios use explicit anchors and fake providers instead of wall-clock drift or real provider calls.",
        },
        {
            "name": "cross_surface_coverage",
            "label": "Cross-surface coverage",
            "summary": "Replay proof must cover memory, workflow, reach, security, and cockpit/operator surfaces together.",
        },
        {
            "name": "failure_taxonomy",
            "label": "Failure taxonomy",
            "summary": "Replay failures are normalized by surface and failure kind so operators can compare memory, reach, workflow, security, and cockpit regressions.",
        },
        {
            "name": "operator_receipts",
            "label": "Operator receipts",
            "summary": "Each replay scenario produces inspectable receipts with evidence, recovery posture, and claim boundaries.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "Live-ish replay proof lives in a named deterministic benchmark suite that can gate regressions quickly.",
        },
    ]


def live_replay_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "memory_replay_drift",
            "surface": "memory",
            "severity": "high",
            "summary": "Long-horizon recall or provider evidence changes behavior without provenance, freshness, or conflict receipts.",
        },
        {
            "name": "workflow_replay_drift",
            "surface": "workflow",
            "severity": "high",
            "summary": "A replayed workflow loses checkpoint, branch, approval, artifact, or recovery continuity.",
        },
        {
            "name": "reach_replay_gap",
            "surface": "reach",
            "severity": "medium",
            "summary": "External-channel replay cannot preserve identity, thread continuity, delivery state, or degraded-route evidence.",
        },
        {
            "name": "security_replay_violation",
            "surface": "security",
            "severity": "critical",
            "summary": "Replay widens trust, leaks sensitive context, crosses a credential boundary, or hides the blocked reason.",
        },
        {
            "name": "cockpit_receipt_gap",
            "surface": "cockpit",
            "severity": "medium",
            "summary": "Operator replay evidence is missing status, surface, recovery posture, or direct drill-in receipts.",
        },
        {
            "name": "provider_nondeterminism",
            "surface": "provider",
            "severity": "medium",
            "summary": "Replay depends on real provider state, live time, or unpinned random output instead of deterministic fixtures.",
        },
    ]


def live_replay_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": LIVE_REPLAY_BENCHMARK_SUITE_NAME,
        "fixture_policy": "fake_providers_and_explicit_time_anchors_required",
        "coverage_policy": "memory_workflow_reach_security_and_cockpit_surfaces_must_all_have_replay_receipts",
        "failure_taxonomy_policy": "surface_failure_recovery_and_claim_boundary_must_be_operator_visible",
        "claim_boundary": "deterministic_liveish_replay_proof_not_live_human_outcome_or_provider_attestation",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/live-long-horizon-replay-benchmark",
            "/api/operator/activity-ledger",
            "/api/operator/workflow-orchestration",
            "/api/operator/guardian-state",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def live_replay_fixture_bundle() -> list[dict[str, Any]]:
    return [
        _receipt(
            surface="memory",
            replay_id="memory-provider-conflict-2026-03-18",
            fake_provider="memory_provider_fixture",
            evidence=["canonical_project_memory", "stale_external_provider_hint", "fresh_session_outcome"],
            expected_failure_modes=["memory_replay_drift"],
            recovery_posture="suppress_stale_provider_and_show_conflict_receipt",
        ),
        _receipt(
            surface="workflow",
            replay_id="workflow-branch-recovery-2026-03-18",
            fake_provider="workflow_replay_fixture",
            evidence=["checkpoint_receipt", "approval_snapshot", "artifact_lineage"],
            expected_failure_modes=["workflow_replay_drift"],
            recovery_posture="restore_checkpoint_or_branch_with_approval_preserved",
        ),
        _receipt(
            surface="reach",
            replay_id="reach-thread-continuity-2026-03-18",
            fake_provider="channel_reach_fixture",
            evidence=["paired_identity", "thread_reference", "delivery_degraded_event"],
            expected_failure_modes=["reach_replay_gap"],
            recovery_posture="degrade_route_and_keep_same_thread_handoff",
        ),
        _receipt(
            surface="security",
            replay_id="security-hostile-provider-2026-03-18",
            fake_provider="hostile_provider_fixture",
            evidence=["blocked_secret_echo", "trust_expansion_attempt", "prompt_injection_marker"],
            expected_failure_modes=["security_replay_violation", "provider_nondeterminism"],
            recovery_posture="block_replay_and_surface_trust_boundary",
        ),
        _receipt(
            surface="cockpit",
            replay_id="cockpit-operator-drilldown-2026-03-18",
            fake_provider="operator_surface_fixture",
            evidence=["benchmark_card", "activity_row", "workflow_orchestration_drilldown"],
            expected_failure_modes=["cockpit_receipt_gap"],
            recovery_posture="show_status_reason_and_direct_drilldown",
        ),
    ]


def _receipt(
    *,
    surface: str,
    replay_id: str,
    fake_provider: str,
    evidence: list[str],
    expected_failure_modes: list[str],
    recovery_posture: str,
) -> dict[str, Any]:
    return {
        "replay_id": replay_id,
        "surface": surface,
        "fake_provider": fake_provider,
        "time_anchor": _TIME_ANCHOR,
        "evidence": evidence,
        "expected_failure_modes": expected_failure_modes,
        "recovery_posture": recovery_posture,
        "operator_visible": True,
        "deterministic": True,
        "claim_boundary": live_replay_policy_payload()["claim_boundary"],
    }


def _live_replay_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Live replay benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_live_replay_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([LIVE_REPLAY_BENCHMARK_SUITE_NAME])


async def build_live_replay_benchmark_report() -> dict[str, Any]:
    summary = await _run_live_replay_benchmark_suite()
    failure_report = _live_replay_failure_report(summary)
    healthy = summary.failed == 0
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": LIVE_REPLAY_BENCHMARK_SUITE_NAME,
            "benchmark_posture": (
                "live_replay_ci_gated_operator_visible"
                if healthy
                else "live_replay_ci_regressions_detected_operator_visible"
            ),
            "operator_status": "live_replay_receipts_visible",
            "scenario_count": len(LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(live_replay_dimensions()),
            "failure_mode_count": len(live_replay_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "fixture_state": "time_stable_fake_provider_replays" if healthy else degraded_state,
            "coverage_state": "memory_workflow_reach_security_cockpit_covered" if healthy else degraded_state,
            "taxonomy_state": "surface_failure_recovery_claim_boundary_visible" if healthy else degraded_state,
            "operator_receipt_state": "benchmark_activity_workflow_guardian_receipts_visible" if healthy else degraded_state,
            "claim_boundary": live_replay_policy_payload()["claim_boundary"],
        },
        "scenario_names": list(LIVE_REPLAY_BENCHMARK_SCENARIO_NAMES),
        "dimensions": live_replay_dimensions(),
        "failure_taxonomy": live_replay_failure_taxonomy(),
        "replay_fixtures": live_replay_fixture_bundle(),
        "failure_report": failure_report,
        "policy": live_replay_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
