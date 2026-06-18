from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from src.extensions.workspace_package import workspace_capability_package_root


GOVERNED_IMPROVEMENT_BENCHMARK_SUITE_NAME = "governed_improvement"
GOVERNED_IMPROVEMENT_BENCHMARK_SCENARIO_NAMES = (
    "governed_self_evolution_behavior",
    "governed_preference_diversity_behavior",
    "governed_canary_rollout_behavior",
    "operator_governed_improvement_benchmark_surface_behavior",
    "capability_repair_behavior",
    "capability_preflight_behavior",
)


def governed_improvement_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "anti_misevolution",
            "label": "Anti-misevolution",
            "summary": "Self-improvement proposals should block obvious drift toward one-size-fits-all behavior before that collapse can enter operator review.",
        },
        {
            "name": "preference_diversity",
            "label": "Preference diversity",
            "summary": "Proposal receipts should preserve minority and user-specific preference nuance instead of rewarding only average-case flattening.",
        },
        {
            "name": "canary_and_rollback",
            "label": "Canary and rollback",
            "summary": "Review candidates should remain canary-only and rollback-ready instead of pretending a saved candidate is already safe to adopt.",
        },
        {
            "name": "operator_safety_receipts",
            "label": "Operator safety receipts",
            "summary": "Operators should be able to inspect proposal gates, blocked constraints, and recent governed-improvement receipts directly from benchmark surfaces.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "Governed self-improvement safeguards should live in a named deterministic suite that can gate regressions.",
        },
    ]


def governed_improvement_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "preference_collapse_regression",
            "severity": "high",
            "summary": "A proposal can flatten user-specific or minority preferences into one generic behavior without being blocked.",
        },
        {
            "name": "ungoverned_candidate_promotion",
            "severity": "high",
            "summary": "A saved candidate can be treated as adoption-ready without an explicit canary stage and review gate.",
        },
        {
            "name": "rollback_receipt_gap",
            "severity": "medium",
            "summary": "Proposal receipts do not preserve enough saved-candidate or receipt-path evidence to support rollback-ready review.",
        },
        {
            "name": "hidden_governed_receipt",
            "severity": "medium",
            "summary": "Operators cannot inspect governed-improvement posture, failure taxonomy, or recent safety receipts from benchmark surfaces.",
        },
        {
            "name": "ungated_governed_regression",
            "severity": "medium",
            "summary": "Governed self-improvement behavior is no longer pinned by a deterministic benchmark suite.",
        },
    ]


def governed_improvement_benchmark_policy_payload() -> dict[str, Any]:
    from src.evolution.engine import evolution_benchmark_gate_policy

    gate_policy = evolution_benchmark_gate_policy()
    return {
        "benchmark_suite": GOVERNED_IMPROVEMENT_BENCHMARK_SUITE_NAME,
        "preference_diversity_policy": "block_preference_collapse_and_watch_single_signal_edits",
        "canary_rollout_policy": str(gate_policy["adoption_policy"]),
        "rollback_policy": str(gate_policy["rollback_policy"]),
        "acceptance_policy": "benchmark_gated_canary_then_reviewed_promotion",
        "operator_visibility": "benchmark_proof_plus_recent_saved_receipts_visible",
        "receipt_surfaces": [
            "/api/evolution/validate",
            "/api/evolution/proposals",
            "/api/operator/benchmark-proof",
            "/api/operator/governed-improvement-benchmark",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def _recent_evolution_receipts(limit: int = 6) -> list[dict[str, Any]]:
    receipts_dir = workspace_capability_package_root() / "evolution" / "receipts"
    if not receipts_dir.exists():
        return []

    receipts: list[dict[str, Any]] = []
    files = sorted(receipts_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[:limit]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        gate = payload.get("benchmark_gate")
        if not isinstance(gate, dict):
            gate = {}
        blocked_constraints = gate.get("blocked_constraints")
        receipts.append(
            {
                "id": path.stem,
                "candidate_name": str(payload.get("candidate_name") or path.stem),
                "target_type": str(payload.get("target_type") or "unknown"),
                "quality_state": str(payload.get("quality_state") or "unknown"),
                "score": float(payload.get("score") or 0.0),
                "rollout_state": str(gate.get("rollout_state") or "unknown"),
                "acceptance_state": str(gate.get("acceptance_state") or "unknown"),
                "diversity_guard_state": str(gate.get("diversity_guard_state") or "unknown"),
                "rollback_ready": bool(gate.get("rollback_ready")),
                "blocked_constraints": [
                    str(item)
                    for item in blocked_constraints
                    if isinstance(item, str)
                ]
                if isinstance(blocked_constraints, list)
                else [],
                "saved_candidate_path": str(gate.get("saved_candidate_path") or payload.get("saved_path") or ""),
                "receipt_path": str(gate.get("receipt_path") or payload.get("receipt_path") or str(path)),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return receipts


def _governed_improvement_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Governed-improvement benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_governed_improvement_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([GOVERNED_IMPROVEMENT_BENCHMARK_SUITE_NAME])


async def build_governed_improvement_benchmark_report() -> dict[str, Any]:
    summary = await _run_governed_improvement_benchmark_suite()
    failure_report = _governed_improvement_failure_report(summary)
    receipts = _recent_evolution_receipts()
    healthy = summary.failed == 0
    held_receipt_count = sum(1 for receipt in receipts if receipt["acceptance_state"] != "ready_for_canary")
    return {
        "summary": {
            "suite_name": GOVERNED_IMPROVEMENT_BENCHMARK_SUITE_NAME,
            "benchmark_posture": (
                "ci_gated_operator_visible"
                if healthy
                else "ci_regressions_detected_operator_visible"
            ),
            "operator_status": "saved_proposal_receipts_visible",
            "scenario_count": len(GOVERNED_IMPROVEMENT_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(governed_improvement_benchmark_dimensions()),
            "failure_mode_count": len(governed_improvement_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "anti_misevolution_state": (
                "preference_collapse_blocked"
                if healthy
                else "regressions_detected"
            ),
            "canary_rollout_state": (
                "review_candidates_canary_only"
                if healthy
                else "regressions_detected"
            ),
            "rollback_state": (
                "candidate_and_receipt_paths_required"
                if healthy
                else "regressions_detected"
            ),
            "operator_receipt_state": "saved_proposal_and_benchmark_receipts_visible",
            "recent_receipt_count": len(receipts),
            "held_receipt_count": held_receipt_count,
        },
        "scenario_names": list(GOVERNED_IMPROVEMENT_BENCHMARK_SCENARIO_NAMES),
        "dimensions": governed_improvement_benchmark_dimensions(),
        "failure_taxonomy": governed_improvement_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": governed_improvement_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
        "recent_receipts": receipts,
    }
