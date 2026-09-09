from __future__ import annotations

from datetime import datetime, timezone
import json
import math
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


def _safe_receipt_score(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return score if math.isfinite(score) else 0.0


def _safe_receipt_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _safe_receipt_text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _safe_receipt_reference(value: Any, *, package_root) -> str:
    from src.evolution.engine import _safe_artifact_reference

    if not value:
        return ""
    try:
        return _safe_artifact_reference(value, package_root=package_root)
    except Exception:
        return "artifact"


def _recent_evolution_receipts(limit: int = 6) -> list[dict[str, Any]]:
    try:
        safe_limit = max(0, int(limit))
    except (TypeError, ValueError, OverflowError):
        safe_limit = 0
    if safe_limit == 0:
        return []

    try:
        package_root = workspace_capability_package_root()
        receipts_dir = package_root / "evolution" / "receipts"
        if not receipts_dir.exists():
            return []
        paths = list(receipts_dir.rglob("*.json"))
    except Exception:
        return []

    receipts: list[dict[str, Any]] = []
    # Receipts are partitioned by target type so identically named candidates
    # cannot overwrite each other's durable evidence.
    files: list[tuple[float, Any]] = []
    for path in paths:
        try:
            modified_at = float(path.stat().st_mtime)
            if not math.isfinite(modified_at):
                continue
            files.append((modified_at, path))
        except Exception:
            continue
    files.sort(key=lambda item: item[0], reverse=True)
    for modified_at, path in files[:safe_limit]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        gate = payload.get("benchmark_gate")
        if not isinstance(gate, dict):
            gate = {}
        lineage = payload.get("lineage")
        if not isinstance(lineage, dict):
            lineage = {}
        blocked_constraints = gate.get("blocked_constraints")
        saved_candidate_reference = _safe_receipt_reference(
            gate.get("saved_candidate_path")
            or payload.get("saved_path")
            or lineage.get("candidate_handle")
            or payload.get("candidate_handle"),
            package_root=package_root,
        )
        receipt_reference = _safe_receipt_reference(
            gate.get("receipt_path")
            or payload.get("receipt_path")
            or lineage.get("receipt_handle")
            or payload.get("receipt_handle")
            or str(path),
            package_root=package_root,
        )
        candidate_handle = _safe_receipt_reference(
            lineage.get("candidate_handle") or payload.get("candidate_handle") or saved_candidate_reference,
            package_root=package_root,
        )
        receipt_handle = _safe_receipt_reference(
            lineage.get("receipt_handle") or payload.get("receipt_handle") or receipt_reference,
            package_root=package_root,
        )
        try:
            updated_at = datetime.fromtimestamp(modified_at, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, TypeError, ValueError):
            updated_at = ""
        receipts.append(
            {
                "id": path.stem,
                "proposal_id": _safe_receipt_text(
                    payload.get("proposal_id") or lineage.get("proposal_id")
                ),
                "candidate_name": _safe_receipt_text(payload.get("candidate_name"), path.stem),
                "target_type": _safe_receipt_text(payload.get("target_type"), "unknown"),
                "source_content_digest": _safe_receipt_text(
                    payload.get("source_content_digest") or lineage.get("source_content_digest")
                ),
                "source_version": _safe_receipt_text(
                    payload.get("source_version") or lineage.get("source_version")
                ),
                "candidate_content_digest": _safe_receipt_text(
                    payload.get("candidate_content_digest") or lineage.get("candidate_content_digest")
                ),
                "candidate_artifact_digest": _safe_receipt_text(
                    payload.get("candidate_artifact_digest") or lineage.get("candidate_artifact_digest")
                ),
                "candidate_handle": candidate_handle,
                "receipt_handle": receipt_handle,
                "quality_state": _safe_receipt_text(payload.get("quality_state"), "unknown"),
                "score": _safe_receipt_score(payload.get("score")),
                "rollout_state": _safe_receipt_text(gate.get("rollout_state"), "unknown"),
                "acceptance_state": _safe_receipt_text(gate.get("acceptance_state"), "unknown"),
                "diversity_guard_state": _safe_receipt_text(gate.get("diversity_guard_state"), "unknown"),
                "rollback_ready": _safe_receipt_bool(gate.get("rollback_ready")),
                "blocked_constraints": [
                    str(item)
                    for item in blocked_constraints
                    if isinstance(item, str)
                ]
                if isinstance(blocked_constraints, list)
                else [],
                "saved_candidate_path": saved_candidate_reference,
                "receipt_path": receipt_reference,
                "updated_at": updated_at,
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
