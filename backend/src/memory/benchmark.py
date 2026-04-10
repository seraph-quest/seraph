from __future__ import annotations

from typing import Any

from src.memory.decay import memory_reconciliation_policy_payload, summarize_memory_reconciliation_state


GUARDIAN_MEMORY_BENCHMARK_SUITE_NAME = "guardian_memory_quality"
GUARDIAN_MEMORY_BENCHMARK_SCENARIO_NAMES = (
    "memory_engineering_retrieval_benchmark_behavior",
    "memory_contradiction_ranking_behavior",
    "memory_selective_forgetting_surface_behavior",
    "operator_memory_benchmark_surface_behavior",
    "memory_provider_user_model_behavior",
    "memory_provider_stale_evidence_behavior",
    "memory_provider_writeback_behavior",
    "memory_reconciliation_policy_behavior",
)


def guardian_memory_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "reasoning_heavy_retrieval",
            "label": "Reasoning-heavy retrieval",
            "summary": "Seraph should recall workflow, approval, artifact, and repo continuity with useful guardian ranking instead of raw snippet match volume.",
        },
        {
            "name": "contradiction_resolution",
            "label": "Contradiction resolution",
            "summary": "Lower-ranked contradictory recall should be suppressed so guardian state does not surface stale competing truth as current memory.",
        },
        {
            "name": "selective_forgetting",
            "label": "Selective forgetting",
            "summary": "Superseded, archived, stale, and irrelevant memory should degrade or disappear explicitly rather than silently lingering in active recall.",
        },
        {
            "name": "operator_visibility",
            "label": "Operator visibility",
            "summary": "Memory benchmark posture, suppression policy, and active failure modes should be visible through operator-facing surfaces instead of hidden behind subjective behavior.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "Guardian memory quality should live in a named benchmark suite that can be rerun and gated like the rest of Seraph's deterministic proof layer.",
        },
    ]


def guardian_memory_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "contradiction_leak",
            "severity": "high",
            "summary": "A lower-ranked contradictory memory survives retrieval and competes with current guardian truth.",
        },
        {
            "name": "stale_memory_leakage",
            "severity": "high",
            "summary": "Superseded or archived memory re-enters active recall without an explicit override or audit trail.",
        },
        {
            "name": "engineering_continuity_miss",
            "severity": "medium",
            "summary": "Workflow, approval, artifact, or repo continuity cues are present but retrieval fails to surface enough engineering memory to be useful.",
        },
        {
            "name": "provider_authority_drift",
            "severity": "medium",
            "summary": "Additive provider evidence widens authority instead of remaining advisory to canonical guardian memory.",
        },
        {
            "name": "hidden_memory_failure",
            "severity": "medium",
            "summary": "Operator surfaces cannot explain why memory failed, decayed, or suppressed a candidate under the current policy.",
        },
    ]


def guardian_memory_benchmark_policy_payload() -> dict[str, Any]:
    reconciliation = memory_reconciliation_policy_payload()
    return {
        **reconciliation,
        "benchmark_suite": GUARDIAN_MEMORY_BENCHMARK_SUITE_NAME,
        "retrieval_ranking_policy": "contradiction_aware_query_and_project_weighted",
        "operator_visibility": "memory_proof_visible",
        "suppression_reasons": [
            "superseded_status",
            "archived_status",
            "lower_ranked_contradiction",
            "stale_provider_evidence",
            "irrelevant_provider_evidence",
        ],
        "engineering_memory_surfaces": [
            "workflow_runs",
            "approvals",
            "audit_receipts",
            "artifacts",
            "repo_pr_continuity",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def _memory_failure_report(summary: dict[str, Any]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    if not isinstance(summary, dict):
        return failures

    for item in summary.get("recent_conflicts", []) or []:
        if not isinstance(item, dict):
            continue
        failures.append(
            {
                "type": "contradiction_reconciled",
                "summary": str(item.get("summary") or "").strip(),
                "reason": str(item.get("reason") or "contradiction").strip(),
            }
        )

    for item in summary.get("recent_archivals", []) or []:
        if not isinstance(item, dict):
            continue
        failures.append(
            {
                "type": "selective_forgetting_archive",
                "summary": str(item.get("summary") or "").strip(),
                "reason": str(item.get("reason") or "archived").strip(),
            }
        )

    error = str(summary.get("error") or "").strip()
    if error:
        failures.append(
            {
                "type": "diagnostic_gap",
                "summary": error,
                "reason": "memory_diagnostics_unavailable",
            }
        )

    return failures[:6]


async def build_guardian_memory_benchmark_report() -> dict[str, Any]:
    reconciliation = await summarize_memory_reconciliation_state()
    failure_report = _memory_failure_report(reconciliation)
    contradiction_state = str(reconciliation.get("state") or "steady")
    return {
        "summary": {
            "suite_name": GUARDIAN_MEMORY_BENCHMARK_SUITE_NAME,
            "benchmark_posture": "ci_gated_operator_visible",
            "operator_status": "memory_proof_visible",
            "scenario_count": len(GUARDIAN_MEMORY_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(guardian_memory_benchmark_dimensions()),
            "failure_mode_count": len(guardian_memory_failure_taxonomy()),
            "active_failure_count": len(failure_report),
            "contradiction_state": contradiction_state,
            "selective_forgetting_state": (
                "active"
                if int(reconciliation.get("archived_count") or 0) or int(reconciliation.get("superseded_count") or 0)
                else "steady"
            ),
        },
        "scenario_names": list(GUARDIAN_MEMORY_BENCHMARK_SCENARIO_NAMES),
        "dimensions": guardian_memory_benchmark_dimensions(),
        "failure_taxonomy": guardian_memory_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": guardian_memory_benchmark_policy_payload(),
        "canonical_memory_reconciliation": reconciliation,
    }
