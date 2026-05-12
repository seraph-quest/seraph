"""Replayable live-workflow endurance canary receipts.

The canary is a deterministic proof artifact over the existing workflow audit
projection. It does not claim a durable workflow state machine.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


LIVE_WORKFLOW_ENDURANCE_CANARY_SUITE_NAME = "live_workflow_endurance_canary"
LIVE_WORKFLOW_ENDURANCE_CANARY_SCENARIO_NAMES = (
    "live_workflow_canary_protocol_behavior",
    "live_workflow_canary_failure_recovery_behavior",
    "live_workflow_canary_approval_preservation_behavior",
    "operator_live_workflow_canary_surface_behavior",
)

LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY = (
    "audit_projected_replayable_canary_not_durable_workflow_engine"
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def live_workflow_endurance_canary_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": LIVE_WORKFLOW_ENDURANCE_CANARY_SUITE_NAME,
        "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        "canary_mode": "deterministic_replay_fixture_over_audit_projected_workflow_state",
        "operator_visibility": "workflow_orchestration_canary_and_benchmark_proof_visible",
        "not_claimed": [
            "durable_workflow_state_machine",
            "crash_safe_resume_executor",
            "heartbeat_or_reactive_trigger_execution",
            "competitor_workflow_superiority",
        ],
        "receipt_surfaces": [
            "/api/operator/live-workflow-endurance-canary",
            "/api/operator/workflow-orchestration",
            "/api/operator/workflow-endurance-benchmark",
            "/api/operator/benchmark-proof",
        ],
        "required_receipts": [
            "run_identity",
            "thread_id",
            "checkpoint_id",
            "branch_lineage",
            "failure_injection",
            "recovery_action",
            "delegated_owner",
            "artifact_comparison",
            "approval_preservation",
            "trust_boundary_decision",
            "audit_trail",
        ],
    }


def live_workflow_endurance_canary_protocol() -> dict[str, Any]:
    return {
        "protocol_id": "seraph-live-workflow-endurance-canary-v1",
        "time_anchor": "2026-05-11T09:00:00Z",
        "seed": "seraph-440-live-workflow-endurance-canary",
        "workflow_name": "live-workflow-endurance-canary",
        "replay_command": (
            "uv run python -m src.evals.harness --benchmark-suite "
            f"{LIVE_WORKFLOW_ENDURANCE_CANARY_SUITE_NAME} --indent 0"
        ),
        "operator_receipt_endpoint": "/api/operator/live-workflow-endurance-canary",
        "fixture_source": "deterministic_canary_receipt",
        "develop_replay_expectation": (
            "after stack merge, run the same benchmark-suite command on develop to reproduce "
            "the audit-projected receipt"
        ),
    }


def live_workflow_endurance_canary_runs() -> list[dict[str, Any]]:
    stable_context = {
        "risk_level": "medium",
        "execution_boundaries": ["workspace_write", "delegation"],
        "accepts_secret_refs": False,
        "authenticated_source": False,
        "delegated_specialists": ["artifact_reviewer"],
        "delegated_tool_names": ["delegate_task"],
        "trust_partition": {
            "owner": "operator",
            "delegated_owner": "artifact_reviewer",
            "mutation_scope": "artifact_review_only",
            "approval_required": True,
        },
    }
    drift_context = {
        **stable_context,
        "authenticated_source": True,
        "execution_boundaries": [
            "workspace_write",
            "delegation",
            "authenticated_external_source",
        ],
    }
    root_run_identity = "session-canary-a:workflow_live_workflow_endurance_canary:root"
    branch_run_identity = "session-canary-a:workflow_live_workflow_endurance_canary:branch-compare"
    recovery_run_identity = "session-canary-a:workflow_live_workflow_endurance_canary:branch-repair"
    approval_run_identity = "session-canary-b:workflow_live_workflow_endurance_canary:approval-preserved"
    drift_run_identity = "session-canary-b:workflow_live_workflow_endurance_canary:approval-drift"
    return [
        {
            "id": "canary-root",
            "tool_name": "workflow_live_workflow_endurance_canary",
            "run_identity": root_run_identity,
            "root_run_identity": root_run_identity,
            "parent_run_identity": None,
            "workflow_name": "live-workflow-endurance-canary",
            "summary": "Primary long-running artifact handoff is paused at comparison before publish.",
            "status": "running",
            "availability": "ready",
            "thread_id": "session-canary-a",
            "thread_label": "Canary artifact handoff",
            "started_at": "2026-05-11T09:00:00Z",
            "updated_at": "2026-05-11T09:37:00Z",
            "approval_context": stable_context,
            "recorded_approval_context": stable_context,
            "current_approval_context": stable_context,
            "approval_context_mismatch": False,
            "pending_approval_count": 0,
            "pending_approval_ids": [],
            "checkpoint_candidates": [
                {
                    "checkpoint_id": "chk-canary-draft",
                    "step_id": "draft",
                    "label": "draft (write_file)",
                    "kind": "branch_from_checkpoint",
                    "status": "succeeded",
                    "resume_supported": True,
                    "resume_draft": (
                        'Run workflow "live-workflow-endurance-canary" with '
                        '_seraph_resume_from_step="draft".'
                    ),
                },
                {
                    "checkpoint_id": "chk-canary-compare",
                    "step_id": "compare",
                    "label": "compare (diff_compare)",
                    "kind": "branch_from_checkpoint",
                    "status": "running",
                    "resume_supported": True,
                    "resume_draft": (
                        'Run workflow "live-workflow-endurance-canary" with '
                        '_seraph_resume_from_step="compare".'
                    ),
                },
            ],
            "artifact_paths": [
                "notes/canary-draft.md",
                "notes/canary-comparison.md",
            ],
            "artifact_receipts": [
                {
                    "artifact_id": "art_canary_draft",
                    "file_path": "notes/canary-draft.md",
                    "content_hash": "sha256:canary-draft-001",
                    "producer": "workflow:live-workflow-endurance-canary",
                    "run_id": root_run_identity,
                },
                {
                    "artifact_id": "art_canary_comparison",
                    "file_path": "notes/canary-comparison.md",
                    "content_hash": "sha256:canary-comparison-001",
                    "producer": "workflow:live-workflow-endurance-canary",
                    "run_id": root_run_identity,
                },
            ],
            "artifact_comparison": {
                "comparison_id": "cmp-canary-root-branch",
                "primary_artifact_id": "art_canary_comparison",
                "branch_artifact_id": "art_canary_branch_comparison",
                "status": "ready",
                "changed_sections": ["risk-table", "approval-notes"],
            },
            "step_records": [
                {"id": "scope", "index": 1, "tool": "session_search", "status": "succeeded"},
                {
                    "id": "delegate",
                    "index": 2,
                    "tool": "delegate_task",
                    "status": "succeeded",
                    "approval_context": stable_context,
                    "delegated_owner": "artifact_reviewer",
                },
                {
                    "id": "draft",
                    "index": 3,
                    "tool": "write_file",
                    "status": "succeeded",
                    "artifact_paths": ["notes/canary-draft.md"],
                },
                {
                    "id": "compare",
                    "index": 4,
                    "tool": "diff_compare",
                    "status": "running",
                    "artifact_paths": ["notes/canary-comparison.md"],
                },
            ],
            "audit_receipts": [
                "audit:canary-root:tool_call",
                "audit:canary-root:tool_result",
            ],
            "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        },
        {
            "id": "canary-branch-compare",
            "tool_name": "workflow_live_workflow_endurance_canary",
            "run_identity": branch_run_identity,
            "root_run_identity": root_run_identity,
            "parent_run_identity": root_run_identity,
            "workflow_name": "live-workflow-endurance-canary",
            "summary": "Checkpoint branch reproduced the comparison and injected a publish failure.",
            "status": "failed",
            "availability": "ready",
            "branch_kind": "branch_from_checkpoint",
            "branch_depth": 1,
            "thread_id": "session-canary-a",
            "thread_label": "Canary artifact handoff",
            "started_at": "2026-05-11T09:38:00Z",
            "updated_at": "2026-05-11T10:02:00Z",
            "approval_context": stable_context,
            "recorded_approval_context": stable_context,
            "current_approval_context": stable_context,
            "approval_context_mismatch": False,
            "pending_approval_count": 0,
            "retry_from_step_draft": (
                'Retry workflow "live-workflow-endurance-canary" from step "publish" '
                "after repairing the injected publish error."
            ),
            "replay_block_reason": None,
            "failure_injection": {
                "injection_id": "fail-canary-publish-timeout",
                "step_id": "publish",
                "error_kind": "InjectedTimeout",
                "operator_reason": "canary_failure_injection",
            },
            "checkpoint_candidates": [
                {
                    "checkpoint_id": "chk-canary-branch-compare",
                    "step_id": "compare",
                    "label": "compare (diff_compare)",
                    "kind": "branch_from_checkpoint",
                    "status": "succeeded",
                    "resume_supported": True,
                    "resume_draft": (
                        'Run workflow "live-workflow-endurance-canary" with '
                        '_seraph_resume_from_step="compare".'
                    ),
                },
            ],
            "artifact_paths": [
                "notes/canary-draft.md",
                "notes/canary-branch-comparison.md",
            ],
            "artifact_receipts": [
                {
                    "artifact_id": "art_canary_branch_comparison",
                    "file_path": "notes/canary-branch-comparison.md",
                    "content_hash": "sha256:canary-comparison-branch-001",
                    "producer": "workflow:live-workflow-endurance-canary",
                    "run_id": branch_run_identity,
                },
            ],
            "artifact_comparison": {
                "comparison_id": "cmp-canary-root-branch",
                "primary_artifact_id": "art_canary_comparison",
                "branch_artifact_id": "art_canary_branch_comparison",
                "status": "divergence_visible",
                "changed_sections": ["risk-table", "approval-notes"],
            },
            "step_records": [
                {"id": "scope", "index": 1, "tool": "session_search", "status": "restored"},
                {"id": "draft", "index": 2, "tool": "write_file", "status": "restored"},
                {
                    "id": "compare",
                    "index": 3,
                    "tool": "diff_compare",
                    "status": "succeeded",
                    "artifact_paths": ["notes/canary-branch-comparison.md"],
                },
                {
                    "id": "publish",
                    "index": 4,
                    "tool": "write_file",
                    "status": "failed",
                    "error_kind": "InjectedTimeout",
                    "error_summary": "Injected timeout while publishing canary handoff.",
                    "recovery_actions": [{"type": "retry_step", "step_id": "publish"}],
                    "is_recoverable": True,
                },
            ],
            "continued_error_steps": ["publish"],
            "audit_receipts": [
                "audit:canary-branch-compare:tool_call",
                "audit:canary-branch-compare:tool_failed",
            ],
            "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        },
        {
            "id": "canary-branch-repair",
            "tool_name": "workflow_live_workflow_endurance_canary",
            "run_identity": recovery_run_identity,
            "root_run_identity": root_run_identity,
            "parent_run_identity": branch_run_identity,
            "workflow_name": "live-workflow-endurance-canary",
            "summary": "Repair branch resumed from publish and preserved artifact review approval.",
            "status": "succeeded",
            "availability": "ready",
            "branch_kind": "retry_failed_step",
            "branch_depth": 2,
            "thread_id": "session-canary-a",
            "thread_label": "Canary artifact handoff",
            "started_at": "2026-05-11T10:03:00Z",
            "updated_at": "2026-05-11T10:18:00Z",
            "approval_context": stable_context,
            "recorded_approval_context": stable_context,
            "current_approval_context": stable_context,
            "approval_context_mismatch": False,
            "approval_preservation": {
                "approval_id": "approval-canary-artifact-review",
                "fingerprint_before": "fp-canary-stable-approval",
                "fingerprint_after": "fp-canary-stable-approval",
                "state_before_recovery": "approved",
                "state_after_recovery": "approved",
                "laundering_blocked": True,
            },
            "recovery_action": {
                "action_id": "repair-canary-publish",
                "from_step": "publish",
                "result": "succeeded",
                "operator_visible": True,
            },
            "artifact_paths": ["notes/canary-final-handoff.md"],
            "artifact_receipts": [
                {
                    "artifact_id": "art_canary_final_handoff",
                    "file_path": "notes/canary-final-handoff.md",
                    "content_hash": "sha256:canary-final-001",
                    "producer": "workflow:live-workflow-endurance-canary",
                    "run_id": recovery_run_identity,
                },
            ],
            "step_records": [
                {"id": "publish", "index": 1, "tool": "write_file", "status": "succeeded"},
                {"id": "audit", "index": 2, "tool": "audit_log", "status": "succeeded"},
            ],
            "audit_receipts": [
                "audit:canary-branch-repair:tool_call",
                "audit:canary-branch-repair:tool_result",
                "audit:canary-branch-repair:approval_preserved",
            ],
            "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        },
        {
            "id": "canary-approval-preserved",
            "tool_name": "workflow_live_workflow_endurance_canary",
            "run_identity": approval_run_identity,
            "root_run_identity": approval_run_identity,
            "parent_run_identity": None,
            "workflow_name": "live-workflow-endurance-canary",
            "summary": "Second session holds a pending operator approval with stable context.",
            "status": "awaiting_approval",
            "availability": "ready",
            "thread_id": "session-canary-b",
            "thread_label": "Canary approval preservation",
            "started_at": "2026-05-11T10:19:00Z",
            "updated_at": "2026-05-11T10:24:00Z",
            "approval_context": stable_context,
            "recorded_approval_context": stable_context,
            "current_approval_context": stable_context,
            "approval_context_mismatch": False,
            "pending_approval_count": 1,
            "pending_approval_ids": ["approval-canary-artifact-review"],
            "approval_preservation": {
                "approval_id": "approval-canary-artifact-review",
                "fingerprint_before": "fp-canary-stable-approval",
                "fingerprint_after": "fp-canary-stable-approval",
                "state_before_recovery": "pending",
                "state_after_recovery": "pending",
                "laundering_blocked": True,
            },
            "checkpoint_candidates": [
                {
                    "checkpoint_id": "chk-canary-approval-gate",
                    "step_id": "approval_gate",
                    "label": "Approval gate",
                    "kind": "approval_resume",
                    "status": "pending",
                    "resume_supported": True,
                    "resume_draft": "Continue the canary after approval.",
                },
            ],
            "step_records": [
                {
                    "id": "approval_gate",
                    "index": 1,
                    "tool": "approval",
                    "status": "awaiting_approval",
                },
            ],
            "audit_receipts": [
                "audit:canary-approval-preserved:tool_call",
                "audit:canary-approval-preserved:approval_pending",
            ],
            "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        },
        {
            "id": "canary-approval-drift",
            "tool_name": "workflow_live_workflow_endurance_canary",
            "run_identity": drift_run_identity,
            "root_run_identity": approval_run_identity,
            "parent_run_identity": approval_run_identity,
            "workflow_name": "live-workflow-endurance-canary",
            "summary": "Replay is blocked because the repair path gained authenticated-source authority.",
            "status": "failed",
            "availability": "blocked",
            "branch_kind": "retry_failed_step",
            "branch_depth": 1,
            "thread_id": "session-canary-b",
            "thread_label": "Canary approval preservation",
            "started_at": "2026-05-11T10:25:00Z",
            "updated_at": "2026-05-11T10:30:00Z",
            "approval_context": drift_context,
            "recorded_approval_context": stable_context,
            "current_approval_context": drift_context,
            "approval_context_mismatch": True,
            "replay_allowed": False,
            "replay_block_reason": "approval_context_changed",
            "trust_boundary": {
                "status": "blocked",
                "reason": "approval_context_changed",
                "recorded_risk_level": "medium",
                "current_risk_level": "medium",
            },
            "checkpoint_candidates": [],
            "step_records": [
                {
                    "id": "publish",
                    "index": 1,
                    "tool": "authenticated_publish",
                    "status": "failed",
                    "error_kind": "ApprovalContextChanged",
                    "error_summary": "Authenticated source was added after approval.",
                    "recovery_actions": [],
                    "is_recoverable": False,
                },
            ],
            "audit_receipts": [
                "audit:canary-approval-drift:tool_call",
                "audit:canary-approval-drift:replay_blocked",
            ],
            "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        },
    ]


def _canary_sessions(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        thread_id = str(run.get("thread_id") or "ambient")
        grouped.setdefault(thread_id, []).append(run)
    sessions: list[dict[str, Any]] = []
    for index, (thread_id, session_runs) in enumerate(sorted(grouped.items()), start=1):
        status_counts = Counter(str(run.get("status") or "unknown") for run in session_runs)
        branch_runs = [
            run for run in session_runs
            if str(run.get("branch_kind") or "") in {"branch_from_checkpoint", "retry_failed_step"}
        ]
        blocked_runs = [
            run for run in session_runs
            if str(run.get("availability") or "") == "blocked"
            or str(run.get("replay_block_reason") or "") == "approval_context_changed"
        ]
        sessions.append({
            "thread_id": thread_id,
            "thread_label": str(session_runs[0].get("thread_label") or thread_id),
            "queue_position": index,
            "workflow_count": len(session_runs),
            "status_counts": dict(sorted(status_counts.items())),
            "branch_run_count": len(branch_runs),
            "blocked_run_count": len(blocked_runs),
            "approval_preserved": any(bool(run.get("approval_preservation")) for run in session_runs),
            "latest_updated_at": max(str(run.get("updated_at") or "") for run in session_runs),
            "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        })
    return sessions


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "type": "benchmark_regression",
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Live workflow endurance canary scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_live_workflow_endurance_canary_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([LIVE_WORKFLOW_ENDURANCE_CANARY_SUITE_NAME])


async def build_live_workflow_endurance_canary_report() -> dict[str, Any]:
    summary = await _run_live_workflow_endurance_canary_suite()
    runs = live_workflow_endurance_canary_runs()
    sessions = _canary_sessions(runs)
    failure_report = _failure_report(summary)
    healthy = summary.failed == 0
    checkpoints = [
        checkpoint
        for run in runs
        for checkpoint in _as_list(run.get("checkpoint_candidates"))
        if isinstance(checkpoint, dict)
    ]
    artifact_receipts = [
        receipt
        for run in runs
        for receipt in _as_list(run.get("artifact_receipts"))
        if isinstance(receipt, dict)
    ]
    approval_preservations = [
        preservation
        for run in runs
        for preservation in [_as_dict(run.get("approval_preservation"))]
        if preservation
    ]
    trust_boundary_blocks = [
        run for run in runs
        if str(run.get("replay_block_reason") or "") == "approval_context_changed"
        or bool(run.get("approval_context_mismatch"))
    ]
    branch_runs = [
        run for run in runs
        if str(run.get("branch_kind") or "") in {"branch_from_checkpoint", "retry_failed_step"}
    ]
    failure_injections = [
        _as_dict(run.get("failure_injection"))
        for run in runs
        if _as_dict(run.get("failure_injection"))
    ]
    recovery_actions = [
        _as_dict(run.get("recovery_action"))
        for run in runs
        if _as_dict(run.get("recovery_action"))
    ]
    audit_receipts = [
        str(receipt)
        for run in runs
        for receipt in _as_list(run.get("audit_receipts"))
        if str(receipt).strip()
    ]
    return {
        "summary": {
            "suite_name": LIVE_WORKFLOW_ENDURANCE_CANARY_SUITE_NAME,
            "benchmark_posture": (
                "live_workflow_canary_ci_gated_operator_visible"
                if healthy
                else "live_workflow_canary_regressions_detected_operator_visible"
            ),
            "operator_status": "live_workflow_canary_visible",
            "scenario_count": len(LIVE_WORKFLOW_ENDURANCE_CANARY_SCENARIO_NAMES),
            "session_count": len(sessions),
            "run_count": len(runs),
            "branch_run_count": len(branch_runs),
            "checkpoint_count": len(checkpoints),
            "failure_injection_count": len(failure_injections),
            "recovery_action_count": len(recovery_actions),
            "artifact_receipt_count": len(artifact_receipts),
            "approval_preservation_count": len(approval_preservations),
            "trust_boundary_block_count": len(trust_boundary_blocks),
            "audit_receipt_count": len(audit_receipts),
            "active_failure_count": summary.failed,
            "claim_boundary": LIVE_WORKFLOW_ENDURANCE_CANARY_CLAIM_BOUNDARY,
        },
        "scenario_names": list(LIVE_WORKFLOW_ENDURANCE_CANARY_SCENARIO_NAMES),
        "protocol": live_workflow_endurance_canary_protocol(),
        "policy": live_workflow_endurance_canary_policy_payload(),
        "sessions": sessions,
        "runs": runs,
        "operator_story": {
            "multi_session_visible": len(sessions) >= 2,
            "delegated_owner_visible": any(
                _as_dict(run.get("approval_context")).get("delegated_specialists")
                for run in runs
            ),
            "checkpoint_branch_visible": bool(checkpoints and branch_runs),
            "failure_recovery_visible": bool(failure_injections and recovery_actions),
            "artifact_comparison_visible": any(
                _as_dict(run.get("artifact_comparison")).get("comparison_id")
                for run in runs
            ),
            "approval_preservation_visible": bool(approval_preservations),
            "trust_boundary_fail_closed_visible": bool(trust_boundary_blocks),
            "audit_trail_visible": len(audit_receipts) >= len(runs),
        },
        "failure_report": failure_report,
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
