"""Minimal durable workflow state kernel receipts.

This module formalizes the state Seraph can already recover from persisted
workflow audit events. It is intentionally smaller than a full workflow engine:
the kernel defines deterministic state snapshots, transitions, trigger receipts,
and artifact-review handoff contracts that operator surfaces and evals can
verify before Seraph claims production durable execution.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import WorkflowArtifactReview, WorkflowRunState, WorkflowStepState
from src.db.session_refs import ensure_sessions_exist


DURABLE_WORKFLOW_ENGINE_SUITE_NAME = "durable_workflow_engine_v1"
DURABLE_WORKFLOW_ENGINE_SCENARIO_NAMES = (
    "durable_workflow_state_kernel_behavior",
    "durable_workflow_crash_safe_resume_behavior",
    "durable_workflow_trigger_heartbeat_reactive_behavior",
    "durable_workflow_retry_repair_transition_behavior",
    "durable_workflow_delegated_artifact_review_behavior",
    "operator_durable_workflow_engine_surface_behavior",
)
DURABLE_WORKFLOW_ENGINE_CLAIM_BOUNDARY = (
    "minimal_durable_state_kernel_not_full_distributed_workflow_engine"
)
DURABLE_STATE_CLAIM_BOUNDARY = DURABLE_WORKFLOW_ENGINE_CLAIM_BOUNDARY
SOURCE_PROJECTION_CLAIM_BOUNDARY = (
    "audit_projected_workflow_receipt_not_durable_state_machine"
)
TRUST_BOUNDARY_BLOCK_REASONS = {"approval_context_changed", "approval_context_missing"}
TERMINAL_STATUSES = {"completed", "succeeded", "failed", "cancelled"}
DURABLE_WORKFLOW_ENGINE_BENCHMARK_SUITE_NAME = DURABLE_WORKFLOW_ENGINE_SUITE_NAME
DURABLE_WORKFLOW_ENGINE_BENCHMARK_SCENARIO_NAMES = DURABLE_WORKFLOW_ENGINE_SCENARIO_NAMES
PRODUCTION_DURABLE_ORCHESTRATION_SUITE_NAME = "production_durable_orchestration"
DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME = "durable_workflow_engine_v2"
DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES = (
    "durable_workflow_v2_lease_ownership_behavior",
    "durable_workflow_v2_idempotent_transition_behavior",
    "durable_workflow_v2_trigger_dedupe_behavior",
    "durable_workflow_v2_recovery_plan_behavior",
    "durable_workflow_v2_delegated_artifact_adoption_behavior",
    "operator_durable_workflow_engine_v2_surface_behavior",
)
PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES = (
    *DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES,
    "production_durable_orchestration_claim_boundary_behavior",
)
DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY = (
    "production_orchestration_receipts_not_langgraph_class_or_exactly_once_engine"
)
PRODUCTION_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY = DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY
DURABLE_WORKFLOW_ENGINE_V2_BLOCKED_CLAIMS = (
    "solved_durable_workflows",
    "langgraph_class_durable_workflows",
    "exactly_once_external_scheduling",
    "crash_proof_orchestration",
    "unbounded_autonomous_multi_agent_orchestration",
    "ahead_of_devin_cursor_workflows",
    "full_workflow_parity",
)
OPERATOR_RECOVERY_TARGET_LABELS = {
    "live_window_receipt_index",
    "safe_checkpoint_before_external_effect",
    "unknown_ack_manual_reconciliation",
    "duplicate_side_effect_attempt",
    "provider_write_unknown_ack",
    "receiver_ack_after_heartbeat_expiry",
    "unsafe_resume_boundary",
    "operator_abort_before_irreversible_boundary",
    "post_dx_orchestration_claim_boundary",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _approval_context_digest(context: dict[str, Any] | None) -> str:
    return hashlib.sha256(json_dumps_canonical(_as_dict(context)).encode("utf-8")).hexdigest()[:20]


def _workflow_v2_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(metadata).copy()
    payload.setdefault("orchestration_v2", {})
    v2 = _as_dict(payload["orchestration_v2"]).copy()
    v2.setdefault("revision", 0)
    v2.setdefault("lease", {})
    v2.setdefault("lease_conflict_receipts", [])
    v2.setdefault("transition_ledger", [])
    v2.setdefault("transition_block_receipts", [])
    v2.setdefault("trigger_ledger", [])
    v2.setdefault("recovery_receipts", [])
    v2.setdefault("artifact_adoption_receipts", [])
    v2.setdefault("restart_recovery_receipts", [])
    v2.setdefault("handoff_receipts", [])
    v2.setdefault("guardian_recovery_receipts", [])
    v2.setdefault("side_effect_boundary_receipts", [])
    v2.setdefault("unsafe_recovery_refusal_receipts", [])
    v2.setdefault("live_window_receipts", [])
    v2.setdefault("failover_receipts", [])
    v2.setdefault("duplicate_suppression_receipts", [])
    v2.setdefault("operator_recovery_control_receipts", [])
    payload["orchestration_v2"] = v2
    return payload


def _lease_active(lease: dict[str, Any], now: datetime | None = None) -> bool:
    expires_at = _parse_iso(lease.get("expires_at"))
    return bool(lease.get("lease_id") and expires_at and expires_at > (now or _utc_now()))


def _stable_id(prefix: str, *parts: Any) -> str:
    seed = "|".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()[:20]


def _safe_operator_recovery_target(target: str) -> dict[str, str]:
    if target in OPERATOR_RECOVERY_TARGET_LABELS:
        return {"target": target, "target_digest": _stable_digest(target)}
    return {
        "target": "redacted_operator_recovery_target",
        "target_digest": _stable_digest(target),
    }


def durable_workflow_snapshot_dict(run: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic durable-state snapshot for one workflow projection."""
    projection = _as_dict(run)
    metadata = _as_dict(projection.get("metadata"))
    artifact_reviews = [
        _as_dict(review)
        for review in _as_list(projection.get("artifact_reviews"))
        if isinstance(review, dict)
    ]
    steps: list[dict[str, Any]] = []
    for fallback_index, raw_step in enumerate(_as_list(projection.get("step_records"))):
        step = _as_dict(raw_step)
        recovery_actions = [
            _as_dict(action)
            for action in _as_list(step.get("recovery_actions"))
            if isinstance(action, dict)
        ]
        steps.append({
            "id": _text(step.get("id"), f"step-{fallback_index}"),
            "index": int(step["index"]) if step.get("index") is not None else fallback_index,
            "tool": _text(step.get("tool"), "unknown"),
            "status": _text(step.get("status"), "unknown"),
            "artifact_paths": _dedupe_sorted(_as_list(step.get("artifact_paths"))),
            "is_recoverable": bool(step.get("is_recoverable")),
            "recovery_actions": recovery_actions,
            "approval_context": _as_dict(step.get("approval_context")),
        })
    steps.sort(key=lambda item: (item["index"], item["id"]))
    artifacts = _dedupe_sorted([
        *_as_list(projection.get("artifact_paths")),
        *[path for step in steps for path in _as_list(step.get("artifact_paths"))],
    ])
    checkpoints = sorted(
        [
            {
                "step_id": _text(item.get("step_id")),
                "label": _text(item.get("label") or item.get("step_id")),
                "status": _text(item.get("status")),
                "resume_draft": item.get("resume_draft"),
            }
            for item in _as_list(projection.get("checkpoint_candidates"))
            if isinstance(item, dict)
        ],
        key=lambda item: (item["step_id"], item["label"]),
    )
    trust_boundary = _as_dict(projection.get("trust_boundary"))
    approval_context = _as_dict(projection.get("approval_context"))
    replay_block_reason = _text(projection.get("replay_block_reason") or trust_boundary.get("reason"))
    trust_blocked = bool(trust_boundary.get("blocked")) or replay_block_reason in TRUST_BOUNDARY_BLOCK_REASONS
    failed_steps = [
        step["id"] for step in steps
        if step["status"] in {"failed", "continued_error", "degraded"}
    ]
    repair_steps = [
        step["id"] for step in steps
        if step["is_recoverable"] or bool(step["recovery_actions"])
    ]
    resume_step = _text(
        projection.get("resume_from_step")
        or (checkpoints[0]["step_id"] if checkpoints else None)
        or (failed_steps[0] if failed_steps else None)
    ) or None
    retry_draft = projection.get("retry_from_step_draft")
    resume_available = bool(
        (resume_step or retry_draft or checkpoints)
        and bool(projection.get("replay_allowed", True))
        and not trust_blocked
    )
    trigger = _as_dict(projection.get("trigger") or projection.get("trigger_receipt"))
    heartbeat_at = trigger.get("heartbeat_at") or projection.get("heartbeat_at") or projection.get("updated_at")
    reactive_source = trigger.get("reactive_source") or projection.get("reactive_source")
    contexts = [approval_context, *[_as_dict(step.get("approval_context")) for step in steps]]
    delegated_specialists = _dedupe_sorted([
        item for context in contexts for item in _as_list(context.get("delegated_specialists"))
    ])
    delegated_tool_names = _dedupe_sorted([
        item for context in contexts for item in _as_list(context.get("delegated_tool_names"))
    ])
    trust_partitions = [
        context["trust_partition"]
        for context in contexts
        if isinstance(context.get("trust_partition"), dict)
    ]
    review = _as_dict(
        projection.get("artifact_review")
        or projection.get("delegated_artifact_review")
        or (artifact_reviews[0] if artifact_reviews else {})
    )
    review_receipts = sorted(
        [
            {
                "artifact_path": _text(item.get("artifact_path")),
                "owner": _text(item.get("owner"), "workflow"),
                "review_state": _text(item.get("review_state"), "pending_review"),
                "reviewer": item.get("reviewer"),
                "decision": item.get("decision"),
                "approval_id": item.get("approval_id"),
                "metadata": _as_dict(item.get("metadata")),
                "lineage": {
                    "run_identity": item.get("run_identity") or projection.get("run_identity"),
                    "root_run_identity": item.get("root_run_identity") or projection.get("root_run_identity") or projection.get("run_identity"),
                    "parent_run_identity": item.get("parent_run_identity") or projection.get("parent_run_identity"),
                },
            }
            for item in artifact_reviews
            if _text(item.get("artifact_path"))
        ],
        key=lambda item: (item["artifact_path"], item["owner"], item["review_state"]),
    )
    audit_receipt_id = _text(
        metadata.get("durable_audit_receipt_id")
        or projection.get("durable_audit_receipt_id")
        or _stable_id("audit_receipt", projection.get("run_identity"), projection.get("status")),
    )
    receipts = {
        "audit": {
            "durable_audit_receipt_id": audit_receipt_id,
            "state_source": _text(projection.get("state_source"), "durable_workflow_state"),
            "summary": metadata.get("summary"),
            "content_redacted": bool(metadata.get("content_redacted", True)),
        },
        "resume": {
            "resume_available": resume_available,
            "resume_from_step": resume_step if resume_available else None,
            "draft": retry_draft if resume_available else None,
            "blocked_reason": replay_block_reason if trust_blocked else None,
            "idempotency_key": _stable_id("resume", projection.get("run_identity"), resume_step or "start"),
        },
        "trigger": {
            "heartbeat_observed": bool(heartbeat_at),
            "heartbeat_at": heartbeat_at,
            "reactive_trigger_observed": bool(reactive_source or trigger.get("type") == "reactive"),
            "reactive_source": reactive_source,
            "implemented_as_receipt_only": True,
        },
        "retry_repair": {
            "retry_available": bool(retry_draft and not trust_blocked),
            "retry_from_step": resume_step,
            "repair_available": bool(repair_steps),
            "failed_step_ids": failed_steps,
            "repair_step_ids": repair_steps,
            "recovery_action_types": _dedupe_sorted([
                action.get("type")
                for step in steps
                for action in _as_list(step.get("recovery_actions"))
                if isinstance(action, dict)
            ]),
        },
        "delegated_artifact_review": {
            "delegation_present": bool(delegated_specialists or delegated_tool_names),
            "artifact_review_present": bool(artifacts or review or review_receipts),
            "delegated_specialists": delegated_specialists,
            "delegated_tool_names": delegated_tool_names,
            "artifact_paths": artifacts,
            "review_state": _text(review.get("review_state") or review.get("state"), "pending" if artifacts else "not_started"),
            "review_count": len(review_receipts),
            "review_receipts": review_receipts,
            "required": bool(review.get("required")) or bool(artifacts and (delegated_specialists or delegated_tool_names)),
            "trust_partitions": trust_partitions,
        },
        "trust_boundary": {
            "blocked": trust_blocked,
            "reason": replay_block_reason or None,
            "requires_fresh_run": bool(trust_boundary.get("requires_fresh_run")),
            "execution_boundaries": _dedupe_sorted(_as_list(approval_context.get("execution_boundaries"))),
        },
        "claim_boundary": {
            "durable_claim_boundary": DURABLE_STATE_CLAIM_BOUNDARY,
            "not_claimed": [
                "workflow_execution",
                "full_distributed_workflow_engine",
                "production_exactly_once_workflow_execution",
                "workflow_execution_without_operator_policy",
            ],
        },
    }
    transitions: list[dict[str, Any]] = [{
        "kind": "run_state",
        "run_identity": projection.get("run_identity"),
        "from": None,
        "to": _text(projection.get("status"), "unknown"),
    }]
    transitions.extend({
        "kind": "step_state",
        "run_identity": projection.get("run_identity"),
        "step_id": step["id"],
        "index": step["index"],
        "from": None,
        "to": step["status"],
    } for step in steps)
    if receipts["resume"]["resume_available"]:
        transitions.append({"kind": "resume_ready", "run_identity": projection.get("run_identity")})
    if receipts["retry_repair"]["retry_available"] or receipts["retry_repair"]["repair_available"]:
        transitions.append({"kind": "repair_ready", "run_identity": projection.get("run_identity")})
    if receipts["trigger"]["heartbeat_observed"] or receipts["trigger"]["reactive_trigger_observed"]:
        transitions.append({"kind": "trigger_observed", "run_identity": projection.get("run_identity")})
    if receipts["delegated_artifact_review"]["artifact_review_present"]:
        transitions.append({"kind": "artifact_review_recorded", "run_identity": projection.get("run_identity")})
    record = {
        "run_identity": projection.get("run_identity"),
        "workflow_name": _text(projection.get("workflow_name"), "workflow"),
        "status": _text(projection.get("status"), "unknown"),
        "audit_receipt_id": audit_receipt_id,
        "session_id": projection.get("thread_id") or projection.get("session_id"),
        "lineage": {
            "root_run_identity": projection.get("root_run_identity") or projection.get("run_identity"),
            "parent_run_identity": projection.get("parent_run_identity"),
            "branch_kind": projection.get("branch_kind"),
            "branch_depth": int(projection.get("branch_depth") or 0),
            "is_branch_run": bool(projection.get("parent_run_identity")),
        },
        "steps": steps,
        "artifact_paths": artifacts,
        "checkpoint_candidates": checkpoints,
        "receipts": receipts,
        "transitions": transitions,
    }
    record["state_hash"] = _canonical_state_id("durable_workflow_state", record)
    snapshot = {"record": record, "claim_boundary": DURABLE_STATE_CLAIM_BOUNDARY}
    snapshot["snapshot_hash"] = _canonical_state_id("durable_workflow_snapshot", snapshot)
    return snapshot


class WorkflowStateRepository:
    def _serialize_run(self, run: WorkflowRunState, steps: list[WorkflowStepState] | None = None) -> dict[str, Any]:
        step_records = [
            self._serialize_step(step)
            for step in sorted(steps or [], key=lambda item: (item.step_index, item.step_id))
        ]
        checkpoint_context = _loads(run.checkpoint_context_json, {})
        return {
            "id": run.id,
            "run_identity": run.run_identity,
            "root_run_identity": run.root_run_identity,
            "parent_run_identity": run.parent_run_identity,
            "workflow_name": run.workflow_name,
            "tool_name": run.tool_name,
            "session_id": run.session_id,
            "status": run.status,
            "branch_kind": run.branch_kind,
            "branch_depth": run.branch_depth,
            "run_fingerprint": run.run_fingerprint,
            "arguments": _loads(run.arguments_json, {}),
            "approval_context": _loads(run.approval_context_json, None),
            "checkpoint_context": checkpoint_context,
            "checkpoint_context_available": bool(checkpoint_context),
            "artifact_paths": _loads(run.artifact_paths_json, []),
            "continued_error_steps": _loads(run.continued_error_steps_json, []),
            "last_completed_step_id": run.last_completed_step_id,
            "error": run.error,
            "heartbeat_at": run.heartbeat_at.isoformat(),
            "started_at": run.started_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "metadata": _loads(run.metadata_json, {}),
            "step_records": step_records,
            "state_source": "durable_workflow_state",
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_CLAIM_BOUNDARY,
        }

    def _serialize_step(self, step: WorkflowStepState) -> dict[str, Any]:
        return {
            "id": step.step_id,
            "index": step.step_index,
            "tool": step.tool_name,
            "status": step.status,
            "arguments": _loads(step.arguments_json, {}),
            "result": _loads(step.result_json, None),
            "result_summary": step.result_summary,
            "artifact_paths": _loads(step.artifact_paths_json, []),
            "error_kind": step.error_kind,
            "error_summary": step.error_summary,
            "checkpoint": _loads(step.checkpoint_json, None),
            "started_at": step.started_at.isoformat(),
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "duration_ms": None,
        }

    def _serialize_review(self, review: WorkflowArtifactReview) -> dict[str, Any]:
        return {
            "id": review.id,
            "run_identity": review.run_identity,
            "root_run_identity": review.root_run_identity,
            "parent_run_identity": review.parent_run_identity,
            "workflow_name": review.workflow_name,
            "artifact_path": review.artifact_path,
            "owner": review.owner,
            "review_state": review.review_state,
            "reviewer": review.reviewer,
            "decision": review.decision,
            "approval_id": review.approval_id,
            "metadata": _loads(review.metadata_json, {}),
            "created_at": review.created_at.isoformat(),
            "updated_at": review.updated_at.isoformat(),
            "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        }

    async def create_run(
        self,
        *,
        run_identity: str,
        workflow_name: str,
        tool_name: str,
        session_id: str | None,
        run_fingerprint: str,
        arguments: dict[str, Any],
        approval_context: dict[str, Any],
        parent_run_identity: str | None = None,
        root_run_identity: str | None = None,
        branch_kind: str | None = None,
        branch_depth: int = 0,
    ) -> dict[str, Any]:
        now = _utc_now()
        async with get_session() as db:
            await ensure_sessions_exist(db, [session_id])
            existing = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            run = existing or WorkflowRunState(
                run_identity=run_identity,
                root_run_identity=root_run_identity or run_identity,
                parent_run_identity=parent_run_identity,
                workflow_name=workflow_name,
                tool_name=tool_name,
                session_id=session_id,
                run_fingerprint=run_fingerprint,
            )
            run.status = "running"
            run.arguments_json = _dumps(arguments)
            run.approval_context_json = _dumps(approval_context)
            run.branch_kind = branch_kind
            run.branch_depth = int(branch_depth or 0)
            run.heartbeat_at = now
            run.updated_at = now
            if existing is None:
                db.add(run)
            await db.flush()
            db.expunge(run)
            return self._serialize_run(run)

    async def record_step_started(
        self,
        *,
        run_identity: str,
        workflow_name: str,
        step_id: str,
        step_index: int,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        now = _utc_now()
        async with get_session() as db:
            existing = (
                await db.execute(
                    select(WorkflowStepState)
                    .where(WorkflowStepState.run_identity == run_identity)
                    .where(WorkflowStepState.step_id == step_id)
                )
            ).scalars().first()
            step = existing or WorkflowStepState(
                run_identity=run_identity,
                workflow_name=workflow_name,
                step_id=step_id,
                step_index=step_index,
                tool_name=tool_name,
            )
            step.status = "running"
            step.arguments_json = _dumps(arguments)
            step.updated_at = now
            if existing is None:
                db.add(step)
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is not None:
                run.heartbeat_at = now
                run.updated_at = now
            await db.flush()
            db.expunge(step)
            return self._serialize_step(step)

    async def record_step_completed(
        self,
        *,
        run_identity: str,
        step_id: str,
        status: str,
        result: Any = None,
        result_summary: str | None = None,
        artifact_paths: list[str] | None = None,
        checkpoint: dict[str, Any] | None = None,
        error_kind: str | None = None,
        error_summary: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        async with get_session() as db:
            step = (
                await db.execute(
                    select(WorkflowStepState)
                    .where(WorkflowStepState.run_identity == run_identity)
                    .where(WorkflowStepState.step_id == step_id)
                )
            ).scalars().first()
            if step is None:
                step = WorkflowStepState(
                    run_identity=run_identity,
                    workflow_name="",
                    step_id=step_id,
                    step_index=0,
                    tool_name="unknown",
                )
                db.add(step)
            step.status = status
            step.result_json = _dumps(result) if result is not None else None
            step.result_summary = result_summary
            step.artifact_paths_json = _dumps(artifact_paths or [])
            step.checkpoint_json = _dumps(checkpoint) if checkpoint is not None else None
            step.error_kind = error_kind
            step.error_summary = error_summary
            step.completed_at = now
            step.updated_at = now
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is not None:
                run.heartbeat_at = now
                run.updated_at = now
            await db.flush()
            db.expunge(step)
            return self._serialize_step(step)

    async def record_step_failed(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("status", "failed")
        return await self.record_step_completed(**kwargs)

    async def finish_run(
        self,
        *,
        run_identity: str,
        status: str,
        checkpoint_context: dict[str, Any] | None = None,
        artifact_paths: list[str] | None = None,
        continued_error_steps: list[str] | None = None,
        last_completed_step_id: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            run.status = status
            run.checkpoint_context_json = _dumps(checkpoint_context or {})
            run.artifact_paths_json = _dumps(artifact_paths or [])
            run.continued_error_steps_json = _dumps(continued_error_steps or [])
            run.last_completed_step_id = last_completed_step_id
            run.error = error
            run.metadata_json = _dumps(metadata or {})
            run.heartbeat_at = now
            run.updated_at = now
            run.finished_at = now if status not in {"running", "awaiting_approval"} else None
            steps = (
                await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == run_identity))
            ).scalars().all()
            await db.flush()
            db.expunge(run)
            for step in steps:
                db.expunge(step)
            return self._serialize_run(run, list(steps))

    async def mark_heartbeat(self, run_identity: str) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            run.heartbeat_at = now
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return self._serialize_run(run)

    async def acquire_or_renew_v2_lease(
        self,
        *,
        run_identity: str,
        owner: str,
        lease_id: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        owner = _text(owner, "workflow-worker")
        lease_id = _text(lease_id) or _stable_id("workflow_lease", run_identity, owner)
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            current_lease = _as_dict(v2.get("lease"))
            active_other_owner = (
                _lease_active(current_lease, now)
                and (
                    current_lease.get("owner") != owner
                    or current_lease.get("lease_id") != lease_id
                )
            )
            receipt = {
                "kind": "lease",
                "run_identity": run_identity,
                "owner": owner,
                "lease_id": lease_id,
                "status": "blocked" if active_other_owner else "acquired",
                "blocked_reason": "active_lease_owned_by_another_worker" if active_other_owner else None,
                "previous_owner": current_lease.get("owner"),
                "expires_at": current_lease.get("expires_at") if active_other_owner else (now + timedelta(seconds=ttl_seconds)).isoformat(),
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            if not active_other_owner:
                v2["revision"] = int(v2.get("revision") or 0) + 1
                v2["lease"] = {
                    "owner": owner,
                    "lease_id": lease_id,
                    "acquired_at": current_lease.get("acquired_at") or now.isoformat(),
                    "renewed_at": now.isoformat(),
                    "expires_at": receipt["expires_at"],
                    "revision": v2["revision"],
                }
                run.heartbeat_at = now
                run.updated_at = now
                run.metadata_json = _dumps(metadata)
                await db.flush()
            else:
                conflicts = [
                    _as_dict(item)
                    for item in _as_list(v2.get("lease_conflict_receipts"))
                    if isinstance(item, dict)
                ]
                conflicts.append(receipt)
                v2["lease_conflict_receipts"] = conflicts[-10:]
                run.updated_at = now
                run.metadata_json = _dumps(metadata)
                await db.flush()
            db.expunge(run)
            return {
                "receipt": receipt,
                "run": self._serialize_run(run),
                "orchestration_v2": v2,
            }

    async def record_v2_transition(
        self,
        *,
        run_identity: str,
        transition_key: str,
        transition_type: str,
        owner: str,
        step_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        transition_key = _text(transition_key) or _stable_id("workflow_transition", run_identity, transition_type, step_id or "run")
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            ledger = [
                _as_dict(item)
                for item in _as_list(v2.get("transition_ledger"))
                if isinstance(item, dict)
            ]
            existing = next((item for item in ledger if item.get("transition_key") == transition_key), None)
            revision = int(v2.get("revision") or 0)
            revision_mismatch = expected_revision is not None and expected_revision != revision
            lease = _as_dict(v2.get("lease"))
            lease_owner_mismatch = not _lease_active(lease, now) or lease.get("owner") != owner
            if lease_owner_mismatch:
                receipt = {
                    "transition_key": transition_key,
                    "transition_type": transition_type,
                    "step_id": step_id,
                    "owner": owner,
                    "status": "blocked",
                    "blocked_reason": "active_owner_lease_required",
                    "lease_owner": lease.get("owner"),
                    "lease_id": lease.get("lease_id"),
                    "operator_visible": True,
                    "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
                }
                blocks = [
                    _as_dict(item)
                    for item in _as_list(v2.get("transition_block_receipts"))
                    if isinstance(item, dict)
                ]
                blocks.append(receipt)
                v2["transition_block_receipts"] = blocks[-10:]
                run.updated_at = now
                run.metadata_json = _dumps(metadata)
                await db.flush()
            elif revision_mismatch:
                receipt = {
                    "transition_key": transition_key,
                    "transition_type": transition_type,
                    "step_id": step_id,
                    "owner": owner,
                    "status": "blocked",
                    "blocked_reason": "revision_mismatch",
                    "expected_revision": expected_revision,
                    "actual_revision": revision,
                    "operator_visible": True,
                    "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
                }
                blocks = [
                    _as_dict(item)
                    for item in _as_list(v2.get("transition_block_receipts"))
                    if isinstance(item, dict)
                ]
                blocks.append(receipt)
                v2["transition_block_receipts"] = blocks[-10:]
                run.updated_at = now
                run.metadata_json = _dumps(metadata)
                await db.flush()
            elif existing:
                receipt = {
                    **existing,
                    "status": "deduped",
                    "dedupe_authority": "lease_owner_and_revision_validated",
                    "operator_visible": True,
                }
            else:
                receipt = {
                    "transition_key": transition_key,
                    "transition_type": transition_type,
                    "step_id": step_id,
                    "owner": owner,
                    "status": "recorded",
                    "revision": revision + 1,
                    "recorded_at": now.isoformat(),
                    "idempotency_key": transition_key,
                    "operator_visible": True,
                    "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
                }
                ledger.append(receipt)
                v2["transition_ledger"] = ledger
                v2["revision"] = revision + 1
                run.heartbeat_at = now
                run.updated_at = now
                run.metadata_json = _dumps(metadata)
                await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_trigger(
        self,
        *,
        run_identity: str,
        trigger_key: str,
        trigger_kind: str,
        source: str,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        trigger_key = _text(trigger_key) or _stable_id("workflow_trigger", run_identity, trigger_kind, source)
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            ledger = [
                _as_dict(item)
                for item in _as_list(v2.get("trigger_ledger"))
                if isinstance(item, dict)
            ]
            existing = next((item for item in ledger if item.get("trigger_key") == trigger_key), None)
            if existing:
                receipt = {**existing, "status": "deduped", "external_action_allowed": False}
            else:
                v2["revision"] = int(v2.get("revision") or 0) + 1
                receipt = {
                    "trigger_key": trigger_key,
                    "trigger_kind": trigger_kind,
                    "source": source,
                    "status": "recorded",
                    "external_action_allowed": False,
                    "authority_required": "recovery_plan_or_operator_resume_required_before_external_action",
                    "next_action": "record_trigger_then_validate_recovery_authority",
                    "recorded_at": now.isoformat(),
                    "revision": v2["revision"],
                    "operator_visible": True,
                    "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
                }
                ledger.append(receipt)
                v2["trigger_ledger"] = ledger
                run.heartbeat_at = now
                run.updated_at = now
                run.metadata_json = _dumps(metadata)
                await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def build_v2_recovery_plan(
        self,
        *,
        run_identity: str,
        owner: str,
        approval_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            stored_context = _loads(run.approval_context_json, {})
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            lease = _as_dict(v2.get("lease"))
            proposed_digest = _approval_context_digest(approval_context or stored_context)
            stored_digest = _approval_context_digest(stored_context)
            context_changed = proposed_digest != stored_digest
            active_other_owner = _lease_active(lease, now) and lease.get("owner") != owner
            blocked_reason = None
            if context_changed:
                blocked_reason = "approval_context_changed"
            elif active_other_owner:
                blocked_reason = "active_lease_owned_by_another_worker"
            receipt = {
                "kind": "recovery_plan",
                "run_identity": run_identity,
                "owner": owner,
                "status": "blocked" if blocked_reason else "ready",
                "blocked_reason": blocked_reason,
                "requires_fresh_run": bool(blocked_reason),
                "resume_from_step": run.last_completed_step_id,
                "root_run_identity": run.root_run_identity,
                "parent_run_identity": run.parent_run_identity,
                "approval_context_digest": stored_digest,
                "proposed_context_digest": proposed_digest,
                "lease_owner": lease.get("owner"),
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            if blocked_reason:
                refusals = [
                    _as_dict(item)
                    for item in _as_list(v2.get("unsafe_recovery_refusal_receipts"))
                    if isinstance(item, dict)
                ]
                refusals.append({
                    "kind": "unsafe_recovery_refusal",
                    "run_identity": run_identity,
                    "owner": owner,
                    "blocked_reason": blocked_reason,
                    "requires_fresh_run": True,
                    "operator_visible": True,
                    "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
                })
                v2["unsafe_recovery_refusal_receipts"] = refusals[-10:]
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("recovery_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["recovery_receipts"] = receipts[-10:]
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_handoff(
        self,
        *,
        run_identity: str,
        from_owner: str,
        to_owner: str,
        receiver_authority_accepted: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            lease = _as_dict(v2.get("lease"))
            revision = int(v2.get("revision") or 0)
            blocked_reason = None
            if not _lease_active(lease, now) or lease.get("owner") != from_owner:
                blocked_reason = "active_owner_lease_required"
            elif expected_revision is not None and expected_revision != revision:
                blocked_reason = "revision_mismatch"
            elif not receiver_authority_accepted:
                blocked_reason = "receiver_authority_not_accepted"
            receipt = {
                "kind": "handoff",
                "run_identity": run_identity,
                "from_owner": from_owner,
                "to_owner": to_owner,
                "status": "blocked" if blocked_reason else "accepted",
                "blocked_reason": blocked_reason,
                "receiver_authority_accepted": receiver_authority_accepted,
                "lease_owner": lease.get("owner"),
                "lease_id_digest": _stable_digest(lease.get("lease_id")),
                "expected_revision": expected_revision,
                "actual_revision": revision,
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            handoffs = [
                _as_dict(item)
                for item in _as_list(v2.get("handoff_receipts"))
                if isinstance(item, dict)
            ]
            handoffs.append(receipt)
            v2["handoff_receipts"] = handoffs[-10:]
            if not blocked_reason:
                v2["revision"] = revision + 1
                v2["lease"] = {
                    **lease,
                    "owner": to_owner,
                    "renewed_at": now.isoformat(),
                    "revision": v2["revision"],
                }
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_side_effect_boundary(
        self,
        *,
        run_identity: str,
        side_effect_kind: str,
        idempotency_scope: str,
        idempotency_key: str,
        external_confirmation_state: str,
        reconciliation_status: str,
        duplicate_suppressed: bool = False,
        redacted_receipt_handle: str | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            receipt = {
                "kind": "side_effect_boundary",
                "run_identity": run_identity,
                "side_effect_kind": side_effect_kind,
                "idempotency_scope": idempotency_scope,
                "idempotency_key_digest": _stable_digest(idempotency_key),
                "external_confirmation_state": external_confirmation_state,
                "reconciliation_status": reconciliation_status,
                "duplicate_suppressed": bool(duplicate_suppressed),
                "redacted_receipt_handle": redacted_receipt_handle
                or f"receipt://workflow/{_stable_id('side_effect', run_identity, side_effect_kind)}",
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("side_effect_boundary_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["side_effect_boundary_receipts"] = receipts[-10:]
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_live_orchestration_window(
        self,
        *,
        run_identity: str,
        provider: str,
        evidence_mode: str,
        window_duration_hours: int,
        expected_fire_count: int,
        observed_fire_count: int,
        max_jitter_ms: int,
        jitter_budget_ms: int,
        residual_risk: str,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            receipt = {
                "kind": "live_orchestration_window",
                "run_identity_digest": _stable_digest(run_identity),
                "provider_digest": _stable_digest(provider),
                "evidence_mode": evidence_mode,
                "window_duration_hours": int(window_duration_hours),
                "expected_fire_count": int(expected_fire_count),
                "observed_fire_count": int(observed_fire_count),
                "max_jitter_ms": int(max_jitter_ms),
                "jitter_budget_ms": int(jitter_budget_ms),
                "within_budget": int(observed_fire_count) >= int(expected_fire_count)
                and int(max_jitter_ms) <= int(jitter_budget_ms),
                "residual_risk": residual_risk,
                "recorded_at": now.isoformat(),
                "raw_receipt_handle": f"receipt://workflow/{_stable_id('live_window', run_identity, provider)}",
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("live_window_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["live_window_receipts"] = receipts[-10:]
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_failover_drill(
        self,
        *,
        run_identity: str,
        failure_mode: str,
        provider: str,
        failover_budget_ms: int,
        observed_failover_ms: int,
        replay_authority: str,
        operator_recovery_control: str,
        restart_preserved_checkpoint: bool = True,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            receipt = {
                "kind": "failover_drill",
                "run_identity_digest": _stable_digest(run_identity),
                "failure_mode": failure_mode,
                "provider_digest": _stable_digest(provider),
                "restart_preserved_checkpoint": bool(restart_preserved_checkpoint),
                "failover_budget_ms": int(failover_budget_ms),
                "observed_failover_ms": int(observed_failover_ms),
                "within_budget": int(observed_failover_ms) <= int(failover_budget_ms),
                "replay_authority": replay_authority,
                "operator_recovery_control": operator_recovery_control,
                "external_action_allowed": False,
                "recorded_at": now.isoformat(),
                "raw_receipt_handle": f"receipt://workflow/{_stable_id('failover', run_identity, failure_mode)}",
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("failover_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["failover_receipts"] = receipts[-10:]
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_duplicate_suppression(
        self,
        *,
        run_identity: str,
        side_effect_kind: str,
        idempotency_key: str,
        duplicate_attempt_count: int,
        suppressed_count: int,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            receipt = {
                "kind": "duplicate_suppression",
                "run_identity_digest": _stable_digest(run_identity),
                "side_effect_kind": side_effect_kind,
                "idempotency_key_digest": _stable_digest(idempotency_key),
                "duplicate_attempt_count": int(duplicate_attempt_count),
                "suppressed_count": int(suppressed_count),
                "all_duplicates_suppressed": int(suppressed_count) >= int(duplicate_attempt_count),
                "external_action_allowed": False,
                "recorded_at": now.isoformat(),
                "redacted_receipt_handle": (
                    f"receipt://workflow/{_stable_id('duplicate_suppression', run_identity, side_effect_kind)}"
                ),
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("duplicate_suppression_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["duplicate_suppression_receipts"] = receipts[-10:]
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_operator_recovery_control(
        self,
        *,
        run_identity: str,
        action: str,
        target: str,
        operator_context: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            safe_target = _safe_operator_recovery_target(target)
            receipt = {
                "kind": "operator_recovery_control",
                "run_identity_digest": _stable_digest(run_identity),
                "action": action,
                "target": safe_target["target"],
                "target_digest": safe_target["target_digest"],
                "enabled": bool(enabled),
                "operator_context_digest": _stable_digest(_as_dict(operator_context)),
                "receipt_after_action": (
                    f"operator-control:{action}:{_stable_id('recovery_control', run_identity, target)}"
                ),
                "external_action_allowed": False,
                "recorded_at": now.isoformat(),
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("operator_recovery_control_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["operator_recovery_control_receipts"] = receipts[-10:]
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_guardian_recovery_context(
        self,
        *,
        run_identity: str,
        guardian_recovery_context: dict[str, Any],
        restraint_posture: str,
        reason_codes: list[str] | None = None,
        authority_expanded: bool = False,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            receipt = {
                "kind": "guardian_recovery",
                "run_identity": run_identity,
                "guardian_recovery_context_digest": _stable_digest(_as_dict(guardian_recovery_context)),
                "restraint_posture": restraint_posture,
                "reason_codes": list(reason_codes or []),
                "authority_expanded": bool(authority_expanded),
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("guardian_recovery_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["guardian_recovery_receipts"] = receipts[-10:]
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def record_v2_artifact_adoption(
        self,
        *,
        run_identity: str,
        artifact_path: str,
        adopter: str,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            reviews = (
                await db.execute(
                    select(WorkflowArtifactReview)
                    .where(WorkflowArtifactReview.run_identity == run_identity)
                    .where(WorkflowArtifactReview.artifact_path == artifact_path)
                )
            ).scalars().all()
            approved_reviews = [
                review
                for review in reviews
                if review.decision in {"approved", "accepted"}
                or review.review_state in {"approved", "accepted"}
            ]
            approved = bool(approved_reviews)
            metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
            v2 = metadata["orchestration_v2"]
            receipt = {
                "kind": "artifact_adoption",
                "run_identity": run_identity,
                "artifact_path": artifact_path,
                "adopter": adopter,
                "status": "recorded" if approved else "blocked",
                "blocked_reason": None if approved else "missing_delegated_artifact_review_approval",
                "review_count": len(reviews),
                "approved_review_count": len(approved_reviews),
                "approval_ids": [review.approval_id for review in approved_reviews if review.approval_id],
                "operator_visible": True,
                "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
            }
            receipts = [
                _as_dict(item)
                for item in _as_list(v2.get("artifact_adoption_receipts"))
                if isinstance(item, dict)
            ]
            receipts.append(receipt)
            v2["artifact_adoption_receipts"] = receipts[-10:]
            v2["revision"] = int(v2.get("revision") or 0) + 1
            run.metadata_json = _dumps(metadata)
            run.updated_at = now
            await db.flush()
            db.expunge(run)
            for review in reviews:
                db.expunge(review)
            return {"receipt": receipt, "run": self._serialize_run(run), "orchestration_v2": v2}

    async def mark_stale_runs_interrupted(self, *, older_than_seconds: int = 300) -> list[dict[str, Any]]:
        cutoff = _utc_now() - timedelta(seconds=older_than_seconds)
        async with get_session() as db:
            result = await db.execute(
                select(WorkflowRunState)
                .where(WorkflowRunState.status == "running")
                .where(WorkflowRunState.heartbeat_at < cutoff)
            )
            runs = result.scalars().all()
            now = _utc_now()
            for run in runs:
                run.status = "interrupted"
                run.updated_at = now
                run.finished_at = now
                metadata = _workflow_v2_metadata(_loads(run.metadata_json, {}))
                v2 = metadata["orchestration_v2"]
                restart_receipts = [
                    _as_dict(item)
                    for item in _as_list(v2.get("restart_recovery_receipts"))
                    if isinstance(item, dict)
                ]
                restart_receipts.append({
                    "kind": "restart_recovery",
                    "run_identity": run.run_identity,
                    "previous_status": "running",
                    "status": "interrupted",
                    "interrupted_by": "durable_workflow_heartbeat",
                    "resume_receipt": True,
                    "preserved_orchestration_v2": True,
                    "lease_id": _as_dict(v2.get("lease")).get("lease_id"),
                    "transition_count": len(_as_list(v2.get("transition_ledger"))),
                    "recovery_count": len(_as_list(v2.get("recovery_receipts"))),
                    "operator_visible": True,
                    "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
                })
                v2["restart_recovery_receipts"] = restart_receipts[-10:]
                metadata["interrupted_by"] = "durable_workflow_heartbeat"
                metadata["resume_receipt"] = True
                run.metadata_json = _dumps(metadata)
            await db.flush()
            for run in runs:
                db.expunge(run)
            return [self._serialize_run(run) for run in runs]

    async def get_checkpoint_payload(self, run_identity: str) -> dict[str, Any] | None:
        async with get_session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_identity))
            ).scalars().first()
            if run is None:
                return None
            steps = (
                await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == run_identity))
            ).scalars().all()
            reviews = (
                await db.execute(select(WorkflowArtifactReview).where(WorkflowArtifactReview.run_identity == run_identity))
            ).scalars().all()
            payload = self._serialize_run(run, list(steps))
            payload["artifact_reviews"] = [self._serialize_review(review) for review in reviews]
            for item in [run, *steps, *reviews]:
                db.expunge(item)
            return {
                "workflow_name": payload["workflow_name"],
                "run_fingerprint": payload["run_fingerprint"],
                "approval_context": payload["approval_context"],
                "step_records": payload["step_records"],
                "checkpoint_step_ids": [step["id"] for step in payload["step_records"]],
                "last_completed_step_id": payload["last_completed_step_id"],
                "artifact_paths": payload["artifact_paths"],
                "continued_error_steps": payload["continued_error_steps"],
                "checkpoint_context": payload["checkpoint_context"],
                "checkpoint_context_available": payload["checkpoint_context_available"],
                "durable_run_identity": payload["run_identity"],
                "state_source": "durable_workflow_state",
            }

    async def list_runs(self, *, limit: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
        async with get_session() as db:
            stmt = select(WorkflowRunState).order_by(col(WorkflowRunState.updated_at).desc()).limit(limit)
            if session_id:
                stmt = stmt.where(WorkflowRunState.session_id == session_id)
            runs = (await db.execute(stmt)).scalars().all()
            serialized: list[dict[str, Any]] = []
            for run in runs:
                steps = (
                    await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == run.run_identity))
                ).scalars().all()
                reviews = (
                    await db.execute(select(WorkflowArtifactReview).where(WorkflowArtifactReview.run_identity == run.run_identity))
                ).scalars().all()
                run_payload = self._serialize_run(run, list(steps))
                run_payload["artifact_reviews"] = [self._serialize_review(review) for review in reviews]
                serialized.append(run_payload)
                for item in [run, *steps, *reviews]:
                    db.expunge(item)
            return serialized

    async def record_artifact_review(
        self,
        *,
        run_identity: str,
        artifact_path: str,
        workflow_name: str = "",
        root_run_identity: str | None = None,
        parent_run_identity: str | None = None,
        owner: str = "workflow",
        review_state: str = "pending_review",
        reviewer: str | None = None,
        decision: str | None = None,
        approval_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        async with get_session() as db:
            review = WorkflowArtifactReview(
                run_identity=run_identity,
                root_run_identity=root_run_identity or run_identity,
                parent_run_identity=parent_run_identity,
                workflow_name=workflow_name,
                artifact_path=artifact_path,
                owner=owner,
                review_state=review_state,
                reviewer=reviewer,
                decision=decision,
                approval_id=approval_id,
                metadata_json=_dumps(metadata or {}),
                decided_at=now if decision else None,
            )
            db.add(review)
            await db.flush()
            db.expunge(review)
            return {
                "id": review.id,
                "run_identity": review.run_identity,
                "root_run_identity": review.root_run_identity,
                "parent_run_identity": review.parent_run_identity,
                "workflow_name": review.workflow_name,
                "artifact_path": review.artifact_path,
                "owner": review.owner,
                "review_state": review.review_state,
                "reviewer": review.reviewer,
                "decision": review.decision,
                "approval_id": review.approval_id,
                "metadata": _loads(review.metadata_json, {}),
                "created_at": review.created_at.isoformat(),
                "updated_at": review.updated_at.isoformat(),
                "decided_at": review.decided_at.isoformat() if review.decided_at else None,
            }


workflow_state_repository = WorkflowStateRepository()


def _canonical_state_id(prefix: str, payload: Any) -> str:
    seed = json_dumps_canonical(payload)
    return f"{prefix}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def json_dumps_canonical(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _dedupe_sorted(values: list[Any]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value or "").strip()})


def durable_workflow_engine_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "durable_state_kernel",
            "label": "Durable state kernel",
            "summary": "Workflow runs project into versioned state snapshots with phase, revision, checkpoint, trigger, and artifact-review fields.",
        },
        {
            "name": "crash_safe_resume",
            "label": "Crash-safe resume",
            "summary": "Resume receipts carry parent run identity, checkpoint step, idempotency key, and trust-boundary block state.",
        },
        {
            "name": "heartbeat_reactive_triggers",
            "label": "Heartbeat and reactive triggers",
            "summary": "Heartbeat and reactive trigger receipts stay deduplicated, policy-scoped, and operator-visible.",
        },
        {
            "name": "retry_repair_lifecycle",
            "label": "Retry and repair lifecycle",
            "summary": "Failed or degraded runs expose retry, repair, and branch transition receipts before continuation.",
        },
        {
            "name": "delegated_artifact_review",
            "label": "Delegated artifact review",
            "summary": "Delegated artifacts preserve owner, lineage, review state, and approval handoff before adoption.",
        },
    ]


def durable_workflow_engine_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "missing_state_snapshot",
            "severity": "high",
            "summary": "A workflow run has no durable state snapshot or revision record.",
        },
        {
            "name": "unsafe_resume_without_checkpoint",
            "severity": "high",
            "summary": "A resume path is offered without a checkpoint, parent identity, and trust-boundary check.",
        },
        {
            "name": "trigger_duplicate_fire",
            "severity": "medium",
            "summary": "Heartbeat or reactive triggers lack an idempotency key and could fire twice after restart.",
        },
        {
            "name": "repair_without_lineage",
            "severity": "medium",
            "summary": "Retry or repair transitions do not link to the failed parent and root workflow identity.",
        },
        {
            "name": "artifact_adopted_without_review",
            "severity": "high",
            "summary": "A delegated artifact can be adopted without an owner, review state, approval handoff, and audit receipt.",
        },
        {
            "name": "overclaimed_durability",
            "severity": "medium",
            "summary": "Operator surfaces imply a full distributed workflow engine instead of the v1 state-kernel boundary.",
        },
    ]


def durable_workflow_engine_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": DURABLE_WORKFLOW_ENGINE_SUITE_NAME,
        "claim_boundary": DURABLE_WORKFLOW_ENGINE_CLAIM_BOUNDARY,
        "durability_policy": "audit_event_backed_state_snapshots_with_versioned_transition_receipts",
        "resume_policy": "checkpoint_resume_requires_parent_identity_checkpoint_idempotency_and_trust_boundary_stability",
        "trigger_policy": "heartbeat_and_reactive_triggers_require_dedupe_keys_and_operator_visible_next_actions",
        "repair_policy": "retry_and_repair_transitions_preserve_root_parent_branch_lineage",
        "artifact_review_policy": "delegated_artifacts_require_owner_review_state_approval_handoff_and_audit_before_adoption",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/durable-workflow-engine",
            "/api/workflows/runs",
            "/api/workflows/runs/{run_identity}/resume-plan",
        ],
        "not_claimed": [
            "full_distributed_workflow_engine",
            "production_crash_recovery_across_all_tools",
            "external_queue_or_scheduler_exactly_once_delivery",
            "live_long_running_autonomous_execution",
            "competitor_superiority",
        ],
    }


def _fixture_workflow_runs() -> list[dict[str, Any]]:
    return [
        {
            "run_identity": "wf_root_research_001",
            "root_run_identity": "wf_root_research_001",
            "workflow_name": "source-progress-review",
            "status": "failed",
            "branch_kind": None,
            "branch_depth": 0,
            "last_completed_step_id": "collect",
            "continued_error_steps": ["synthesize"],
            "checkpoint_candidates": [
                {
                    "step_id": "synthesize",
                    "label": "Resume from synthesize",
                    "status": "succeeded",
                    "resume_supported": True,
                    "resume_draft": {"workflow_name": "source-progress-review"},
                }
            ],
            "retry_from_step_draft": {"workflow_name": "source-progress-review", "step_id": "synthesize"},
            "artifact_paths": ["reports/source-progress.md"],
            "pending_approval_count": 0,
            "trust_boundary": {"status": "stable", "blocked": False},
            "approval_context": {"risk_level": "medium", "execution_boundaries": ["workspace_filesystem"]},
        },
        {
            "run_identity": "wf_retry_research_002",
            "root_run_identity": "wf_root_research_001",
            "parent_run_identity": "wf_root_research_001",
            "workflow_name": "source-progress-review",
            "status": "succeeded",
            "branch_kind": "retry_failed_step",
            "branch_depth": 1,
            "resume_from_step": "synthesize",
            "last_completed_step_id": "publish",
            "continued_error_steps": [],
            "checkpoint_candidates": [],
            "retry_from_step_draft": None,
            "artifact_paths": ["reports/source-progress.md", "reports/source-progress.repaired.md"],
            "pending_approval_count": 1,
            "trust_boundary": {"status": "stable", "blocked": False},
            "approval_context": {
                "risk_level": "medium",
                "execution_boundaries": ["workspace_filesystem", "delegation"],
                "delegated_specialists": ["research"],
            },
        },
    ]


def _state_phase(run: dict[str, Any]) -> str:
    status = str(run.get("status") or "unknown")
    if int(run.get("pending_approval_count") or 0) > 0:
        return "awaiting_operator_review"
    if status == "failed":
        return "repairable_failure"
    if status == "running":
        return "running"
    if status in {"succeeded", "degraded"}:
        return "completed_with_receipts"
    return status


def _checkpoint_receipt(run: dict[str, Any]) -> dict[str, Any]:
    candidates = [item for item in run.get("checkpoint_candidates", []) if isinstance(item, dict)]
    checkpoint = candidates[0] if candidates else {}
    checkpoint_context_available = bool(run.get("checkpoint_context_available"))
    step_id = str(
        checkpoint.get("step_id")
        or run.get("resume_from_step")
        or run.get("last_completed_step_id")
        or ""
    )
    available = bool(candidates or run.get("resume_from_step") or (checkpoint_context_available and step_id))
    return {
        "checkpoint_id": _stable_id("checkpoint", run.get("run_identity"), step_id),
        "step_id": step_id or None,
        "available": available,
        "resume_supported": bool(
            checkpoint.get("resume_supported", bool(run.get("resume_from_step") or checkpoint_context_available))
        ),
        "checkpoint_count": len(candidates),
        "checkpoint_context_available": checkpoint_context_available,
        "trust_boundary_stable": not bool((run.get("trust_boundary") or {}).get("blocked")),
    }


def _resume_receipt(run: dict[str, Any]) -> dict[str, Any]:
    checkpoint = _checkpoint_receipt(run)
    parent = str(run.get("parent_run_identity") or run.get("run_identity") or "")
    step_id = str(checkpoint.get("step_id") or "")
    trust_blocked = bool((run.get("trust_boundary") or {}).get("blocked"))
    return {
        "resume_ready": bool(checkpoint["available"] and parent and not trust_blocked),
        "parent_run_identity": parent,
        "root_run_identity": run.get("root_run_identity") or run.get("run_identity"),
        "resume_from_step": step_id or None,
        "idempotency_key": _stable_id("resume", parent, step_id or "start"),
        "trust_boundary_blocked": trust_blocked,
        "blocked_reason": "trust_boundary_changed" if trust_blocked else None,
        "crash_safe": bool(checkpoint["available"] and parent and checkpoint["trust_boundary_stable"]),
    }


def _trigger_receipts(run: dict[str, Any]) -> list[dict[str, Any]]:
    root = str(run.get("root_run_identity") or run.get("run_identity") or "workflow")
    return [
        {
            "kind": "heartbeat",
            "state": "armed",
            "dedupe_key": _stable_id("trigger", root, "heartbeat"),
            "next_action": "resume_or_repair_if_stalled",
            "operator_visible": True,
        },
        {
            "kind": "reactive_signal",
            "state": "armed",
            "dedupe_key": _stable_id("trigger", root, "artifact_review"),
            "next_action": "surface_delegated_artifact_review",
            "operator_visible": True,
        },
    ]


def _artifact_review_receipts(run: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = [path for path in run.get("artifact_paths", []) if isinstance(path, str) and path.strip()]
    delegated = bool((run.get("approval_context") or {}).get("delegated_specialists"))
    recorded_reviews = [
        item for item in run.get("artifact_reviews", []) if isinstance(item, dict)
    ]
    receipts: list[dict[str, Any]] = []
    for review in recorded_reviews:
        path = str(review.get("artifact_path") or "").strip()
        if not path:
            continue
        receipts.append(
            {
                "artifact_path": path,
                "owner": str(review.get("owner") or "workflow"),
                "review_state": str(review.get("review_state") or "pending_review"),
                "approval_handoff": bool(review.get("approval_id") or review.get("reviewer")),
                "lineage": {
                    "run_identity": review.get("run_identity") or run.get("run_identity"),
                    "root_run_identity": review.get("root_run_identity") or run.get("root_run_identity") or run.get("run_identity"),
                    "parent_run_identity": review.get("parent_run_identity") or run.get("parent_run_identity"),
                },
                "audit_receipt_id": str(
                    (review.get("metadata") or {}).get("durable_audit_receipt_id")
                    or _stable_id("artifact_review", review.get("run_identity") or run.get("run_identity"), path)
                ),
            }
        )
    for path in artifacts:
        if any(receipt["artifact_path"] == path for receipt in receipts):
            continue
        receipts.append(
            {
                "artifact_path": path,
                "owner": "delegated_specialist" if delegated else "workflow",
                "review_state": "pending_operator_review" if delegated or run.get("pending_approval_count") else "self_reviewed",
                "approval_handoff": bool(delegated or run.get("pending_approval_count")),
                "lineage": {
                    "run_identity": run.get("run_identity"),
                    "root_run_identity": run.get("root_run_identity") or run.get("run_identity"),
                    "parent_run_identity": run.get("parent_run_identity"),
                },
                "audit_receipt_id": _stable_id("artifact_review", run.get("run_identity"), path),
            }
        )
    return receipts


def build_durable_workflow_state_kernel(
    workflow_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runs = workflow_runs or _fixture_workflow_runs()
    states: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    for revision, run in enumerate(runs, start=1):
        run_identity = str(run.get("run_identity") or f"workflow-{revision}")
        phase = _state_phase(run)
        checkpoint = _checkpoint_receipt(run)
        resume = _resume_receipt(run)
        triggers = _trigger_receipts(run)
        artifact_reviews = _artifact_review_receipts(run)
        state = {
            "state_id": _stable_id("workflow_state", run_identity, revision),
            "durable_state_version": "2026.05.v1",
            "revision": revision,
            "run_identity": run_identity,
            "root_run_identity": run.get("root_run_identity") or run_identity,
            "parent_run_identity": run.get("parent_run_identity"),
            "workflow_name": run.get("workflow_name"),
            "status": run.get("status"),
            "phase": phase,
            "branch_kind": run.get("branch_kind"),
            "branch_depth": int(run.get("branch_depth") or 0),
            "checkpoint": checkpoint,
            "resume": resume,
            "triggers": triggers,
            "retry": {
                "retry_ready": bool(run.get("retry_from_step_draft")),
                "draft": run.get("retry_from_step_draft"),
                "repair_required": phase == "repairable_failure",
            },
            "repair": {
                "repair_ready": bool(run.get("retry_from_step_draft") or run.get("continued_error_steps")),
                "failed_step_ids": [str(item) for item in run.get("continued_error_steps", [])],
                "lineage_preserved": bool(run.get("root_run_identity") or run.get("parent_run_identity")),
            },
            "artifact_review": {
                "artifact_count": len(artifact_reviews),
                "pending_review_count": sum(
                    1 for item in artifact_reviews if item["review_state"] == "pending_operator_review"
                ),
                "receipts": artifact_reviews,
            },
            "audit": {
                "durable_audit_receipt_id": str(
                    (run.get("metadata") or {}).get("durable_audit_receipt_id")
                    or _stable_id("audit_receipt", run_identity, run.get("status"))
                ),
                "state_source": str(run.get("state_source") or "durable_workflow_state"),
                "content_redacted": bool((run.get("metadata") or {}).get("content_redacted", True)),
            },
            "trust_boundary": run.get("trust_boundary") or {"status": "unknown", "blocked": False},
            "source": "audit_event_journal_plus_state_kernel_projection",
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_CLAIM_BOUNDARY,
        }
        states.append(state)
        if revision == 1:
            transitions.append(
                {
                    "transition_id": _stable_id("transition", run_identity, "recorded"),
                    "from": "recorded",
                    "to": phase,
                    "reason": "workflow_run_projected",
                    "durable": True,
                    "operator_visible": True,
                }
            )
        else:
            previous = states[revision - 2]
            transitions.append(
                {
                    "transition_id": _stable_id("transition", previous["run_identity"], run_identity),
                    "from": previous["phase"],
                    "to": phase,
                    "reason": str(run.get("branch_kind") or "state_update"),
                    "parent_run_identity": run.get("parent_run_identity"),
                    "root_run_identity": run.get("root_run_identity") or run_identity,
                    "durable": True,
                    "operator_visible": True,
                }
            )
    return {
        "summary": {
            "state_count": len(states),
            "transition_count": len(transitions),
            "kernel_posture": "durable_state_kernel_receipts_visible",
            "crash_resume_ready_count": sum(1 for state in states if state["resume"]["crash_safe"]),
            "trigger_receipt_count": sum(len(state["triggers"]) for state in states),
            "pending_artifact_review_count": sum(
                state["artifact_review"]["pending_review_count"] for state in states
            ),
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_CLAIM_BOUNDARY,
        },
        "states": states,
        "transitions": transitions,
        "policy": durable_workflow_engine_policy_payload(),
    }


def _durable_workflow_engine_failure_report(summary: Any) -> list[dict[str, str]]:
    failed = int(getattr(summary, "failed", 0) or 0)
    if failed == 0:
        return []
    return [
        {
            "suite": DURABLE_WORKFLOW_ENGINE_SUITE_NAME,
            "failed": str(failed),
            "summary": "Durable workflow engine benchmark reported regressions.",
        }
    ]


async def _run_durable_workflow_engine_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([DURABLE_WORKFLOW_ENGINE_SUITE_NAME])


async def build_durable_workflow_state_report() -> dict[str, Any]:
    summary = await _run_durable_workflow_engine_suite()
    failure_report = _durable_workflow_engine_failure_report(summary)
    proof_kernel = build_durable_workflow_state_kernel()
    try:
        persisted_runs = await workflow_state_repository.list_runs(limit=10)
    except Exception:  # pragma: no cover - defensive fail-soft path for partially migrated/local DBs.
        persisted_runs = []
    persisted_kernel = (
        build_durable_workflow_state_kernel(persisted_runs)
        if persisted_runs
        else {"summary": {"state_count": 0, "transition_count": 0}, "states": [], "transitions": [], "policy": durable_workflow_engine_policy_payload()}
    )
    persisted_snapshots = [
        durable_workflow_snapshot_dict(run)
        for run in persisted_runs
        if isinstance(run, dict)
    ]
    active_kernel = persisted_kernel if persisted_runs else proof_kernel
    return {
        "summary": {
            "suite_name": DURABLE_WORKFLOW_ENGINE_SUITE_NAME,
            "operator_status": "durable_workflow_state_kernel_receipts_visible",
            "benchmark_posture": (
                "durable_workflow_engine_ci_gated_operator_visible"
                if not failure_report
                else "durable_workflow_engine_regressions_detected"
            ),
            "scenario_count": len(DURABLE_WORKFLOW_ENGINE_SCENARIO_NAMES),
            "dimension_count": len(durable_workflow_engine_dimensions()),
            "failure_mode_count": len(durable_workflow_engine_failure_taxonomy()),
            "state_count": active_kernel["summary"]["state_count"],
            "transition_count": active_kernel["summary"]["transition_count"],
            "persisted_run_count": len(persisted_runs),
            "persisted_snapshot_count": len(persisted_snapshots),
            "proof_fixture_state_count": proof_kernel["summary"]["state_count"],
            "crash_resume_ready_count": active_kernel["summary"].get("crash_resume_ready_count", 0),
            "pending_artifact_review_count": active_kernel["summary"].get("pending_artifact_review_count", 0),
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_CLAIM_BOUNDARY,
        },
        "scenario_names": list(DURABLE_WORKFLOW_ENGINE_SCENARIO_NAMES),
        "dimensions": durable_workflow_engine_dimensions(),
        "failure_taxonomy": durable_workflow_engine_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": durable_workflow_engine_policy_payload(),
        "state_kernel": active_kernel,
        "proof_state_kernel": proof_kernel,
        "persisted_state_kernel": persisted_kernel,
        "persisted_run_snapshots": persisted_snapshots,
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }


def durable_workflow_v2_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_DURABLE_ORCHESTRATION_SUITE_NAME,
            DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME,
        ],
        "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
        "durability_policy": "lease_owned_revision_guarded_recovery_receipts_on_existing_durable_state",
        "lease_policy": "active_worker_or_delegated_agent_requires_non_expired_owner_lease_before_continuation",
        "transition_policy": "step_resume_retry_repair_and_trigger_paths_require_idempotency_keys_and_revision_guards",
        "recovery_policy": "unsafe_resume_blocks_on_approval_trust_credential_owner_or_checkpoint_drift",
        "artifact_policy": "delegated_artifacts_require review_or_approval_receipt_before_adoption",
        "operator_surfaces": [
            "/api/operator/durable-workflow-engine-v2",
            "/api/operator/durable-workflow-engine",
            "/api/operator/benchmark-proof",
        ],
        "blocked_claims": list(DURABLE_WORKFLOW_ENGINE_V2_BLOCKED_CLAIMS),
        "not_claimed": [
            "langgraph_class_durable_workflows",
            "exactly_once_external_scheduling",
            "crash_proof_orchestration",
            "unbounded_autonomous_multi_agent_orchestration",
            "full_workflow_parity",
        ],
    }


def _fixture_workflow_v2_runs() -> list[dict[str, Any]]:
    return [
        {
            "run_identity": "wf_v2_release_root",
            "root_run_identity": "wf_v2_release_root",
            "workflow_name": "release-hardening",
            "status": "interrupted",
            "last_completed_step_id": "collect",
            "approval_context": {
                "risk_level": "medium",
                "execution_boundaries": ["workspace_filesystem", "delegation"],
                "delegated_specialists": ["critic"],
            },
            "metadata": {
                "orchestration_v2": {
                    "revision": 4,
                    "lease": {
                        "owner": "worker-a",
                        "lease_id": "lease-release-worker-a",
                        "acquired_at": "2026-06-09T10:00:00+00:00",
                        "renewed_at": "2026-06-09T10:04:00+00:00",
                        "expires_at": "2026-06-09T10:09:00+00:00",
                        "revision": 4,
                    },
                    "lease_conflict_receipts": [
                        {
                            "kind": "lease",
                            "owner": "worker-b",
                            "lease_id": "lease-release-worker-a",
                            "status": "blocked",
                            "blocked_reason": "active_lease_owned_by_another_worker",
                            "previous_owner": "worker-a",
                            "operator_visible": True,
                        }
                    ],
                    "transition_ledger": [
                        {
                            "transition_key": "resume:collect",
                            "transition_type": "resume",
                            "step_id": "collect",
                            "owner": "worker-a",
                            "status": "recorded",
                            "revision": 3,
                            "idempotency_key": "resume:collect",
                        }
                    ],
                    "transition_block_receipts": [
                        {
                            "transition_key": "resume:publish",
                            "transition_type": "resume",
                            "step_id": "publish",
                            "owner": "worker-b",
                            "status": "blocked",
                            "blocked_reason": "active_owner_lease_required",
                            "lease_owner": "worker-a",
                            "lease_id": "lease-release-worker-a",
                            "operator_visible": True,
                        }
                    ],
                    "trigger_ledger": [
                        {
                            "trigger_key": "heartbeat:wf_v2_release_root",
                            "trigger_kind": "heartbeat",
                            "source": "scheduler",
                            "status": "recorded",
                            "external_action_allowed": True,
                        }
                    ],
                    "recovery_receipts": [
                        {
                            "kind": "recovery_plan",
                            "status": "ready",
                            "resume_from_step": "collect",
                            "operator_visible": True,
                        }
                    ],
                    "artifact_adoption_receipts": [
                        {
                            "kind": "artifact_adoption",
                            "artifact_path": "reports/release.md",
                            "status": "recorded",
                            "approval_ids": ["approval-release-review"],
                        }
                    ],
                }
            },
            "artifact_reviews": [
                {
                    "artifact_path": "reports/release.md",
                    "owner": "delegated_specialist",
                    "review_state": "approved",
                    "approval_id": "approval-release-review",
                }
            ],
        },
        {
            "run_identity": "wf_v2_blocked_auth",
            "root_run_identity": "wf_v2_blocked_auth",
            "workflow_name": "authenticated-brief",
            "status": "failed",
            "last_completed_step_id": "fetch-private-source",
            "approval_context": {
                "risk_level": "high",
                "execution_boundaries": ["external_mcp_credential_egress"],
                "accepts_secret_refs": True,
            },
            "metadata": {
                "orchestration_v2": {
                    "revision": 2,
                    "lease": {
                        "owner": "worker-b",
                        "lease_id": "lease-auth-worker-b",
                        "expires_at": "2026-06-09T10:09:00+00:00",
                    },
                    "transition_ledger": [],
                    "trigger_ledger": [
                        {
                            "trigger_key": "reactive:private-source",
                            "trigger_kind": "reactive_signal",
                            "source": "private-source",
                            "status": "deduped",
                            "external_action_allowed": False,
                        }
                    ],
                    "recovery_receipts": [
                        {
                            "kind": "recovery_plan",
                            "status": "blocked",
                            "blocked_reason": "approval_context_changed",
                            "requires_fresh_run": True,
                            "operator_visible": True,
                        }
                    ],
                    "artifact_adoption_receipts": [
                        {
                            "kind": "artifact_adoption",
                            "artifact_path": "reports/private.md",
                            "status": "blocked",
                            "blocked_reason": "missing_delegated_artifact_review_approval",
                        }
                    ],
                }
            },
        },
    ]


def build_durable_workflow_v2_contract(
    workflow_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runs = workflow_runs or _fixture_workflow_v2_runs()
    receipts: list[dict[str, Any]] = []
    for run in runs:
        metadata = _workflow_v2_metadata(_as_dict(run.get("metadata")))
        v2 = _as_dict(metadata.get("orchestration_v2"))
        lease = _as_dict(v2.get("lease"))
        transitions = [
            _as_dict(item)
            for item in _as_list(v2.get("transition_ledger"))
            if isinstance(item, dict)
        ]
        lease_conflicts = [
            _as_dict(item)
            for item in _as_list(v2.get("lease_conflict_receipts"))
            if isinstance(item, dict)
        ]
        transition_blocks = [
            _as_dict(item)
            for item in _as_list(v2.get("transition_block_receipts"))
            if isinstance(item, dict)
        ]
        triggers = [
            _as_dict(item)
            for item in _as_list(v2.get("trigger_ledger"))
            if isinstance(item, dict)
        ]
        recoveries = [
            _as_dict(item)
            for item in _as_list(v2.get("recovery_receipts"))
            if isinstance(item, dict)
        ]
        adoptions = [
            _as_dict(item)
            for item in _as_list(v2.get("artifact_adoption_receipts"))
            if isinstance(item, dict)
        ]
        receipts.append({
            "run_identity": run.get("run_identity"),
            "root_run_identity": run.get("root_run_identity") or run.get("run_identity"),
            "parent_run_identity": run.get("parent_run_identity"),
            "workflow_name": run.get("workflow_name"),
            "status": run.get("status"),
            "revision": int(v2.get("revision") or 0),
            "lease": {
                "owner": lease.get("owner"),
                "lease_id": lease.get("lease_id"),
                "expires_at": lease.get("expires_at"),
                "operator_visible": bool(lease.get("lease_id")),
            },
            "lease_conflict_count": len(lease_conflicts),
            "latest_lease_conflict": lease_conflicts[-1] if lease_conflicts else {},
            "transition_count": len(transitions),
            "transition_keys": _dedupe_sorted([item.get("transition_key") for item in transitions]),
            "transition_block_count": len(transition_blocks),
            "latest_transition_block": transition_blocks[-1] if transition_blocks else {},
            "trigger_count": len(triggers),
            "deduped_trigger_count": sum(1 for item in triggers if item.get("status") == "deduped"),
            "recovery": recoveries[-1] if recoveries else {},
            "artifact_adoption": adoptions[-1] if adoptions else {},
            "approval_context_digest": _approval_context_digest(_as_dict(run.get("approval_context"))),
            "blocked_claims": list(DURABLE_WORKFLOW_ENGINE_V2_BLOCKED_CLAIMS),
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
        })
    blocked_recoveries = [
        receipt for receipt in receipts
        if _as_dict(receipt.get("recovery")).get("status") == "blocked"
    ]
    blocked_adoptions = [
        receipt for receipt in receipts
        if _as_dict(receipt.get("artifact_adoption")).get("status") == "blocked"
    ]
    return {
        "summary": {
            "suite_name": DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME,
            "operator_status": "durable_workflow_engine_v2_recovery_receipts_visible",
            "run_count": len(receipts),
            "lease_receipt_count": sum(1 for item in receipts if item["lease"]["operator_visible"]),
            "blocked_lease_count": sum(int(item["lease_conflict_count"]) for item in receipts),
            "transition_receipt_count": sum(int(item["transition_count"]) for item in receipts),
            "blocked_transition_count": sum(int(item["transition_block_count"]) for item in receipts),
            "trigger_receipt_count": sum(int(item["trigger_count"]) for item in receipts),
            "deduped_trigger_count": sum(int(item["deduped_trigger_count"]) for item in receipts),
            "blocked_recovery_count": len(blocked_recoveries),
            "blocked_artifact_adoption_count": len(blocked_adoptions),
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
        },
        "receipts": receipts,
        "policy": durable_workflow_v2_policy_payload(),
    }


def _durable_workflow_engine_v2_failure_report(summary: Any) -> list[dict[str, str]]:
    failed = int(getattr(summary, "failed", 0) or 0)
    if failed == 0:
        return []
    return [
        {
            "suite": DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME,
            "failed": str(failed),
            "summary": "Durable workflow engine v2 benchmark reported regressions.",
        }
    ]


async def _run_durable_workflow_engine_v2_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_DURABLE_ORCHESTRATION_SUITE_NAME,
        DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME,
    ])


async def build_durable_workflow_v2_report() -> dict[str, Any]:
    summary = await _run_durable_workflow_engine_v2_suites()
    failure_report = _durable_workflow_engine_v2_failure_report(summary)
    try:
        persisted_runs = await workflow_state_repository.list_runs(limit=10)
    except Exception:  # pragma: no cover - defensive fail-soft path for partially migrated/local DBs.
        persisted_runs = []
    proof_contract = build_durable_workflow_v2_contract()
    persisted_contract = build_durable_workflow_v2_contract(persisted_runs) if persisted_runs else {
        "summary": {
            "run_count": 0,
            "lease_receipt_count": 0,
            "blocked_lease_count": 0,
            "transition_receipt_count": 0,
            "blocked_transition_count": 0,
            "trigger_receipt_count": 0,
            "deduped_trigger_count": 0,
            "blocked_recovery_count": 0,
            "blocked_artifact_adoption_count": 0,
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
        },
        "receipts": [],
        "policy": durable_workflow_v2_policy_payload(),
    }
    active_contract = persisted_contract if persisted_runs else proof_contract
    return {
        "summary": {
            "suite_name": DURABLE_WORKFLOW_ENGINE_V2_SUITE_NAME,
            "production_suite_name": PRODUCTION_DURABLE_ORCHESTRATION_SUITE_NAME,
            "operator_status": "durable_workflow_engine_v2_recovery_receipts_visible",
            "benchmark_posture": (
                "durable_workflow_engine_v2_ci_gated_operator_visible"
                if not failure_report
                else "durable_workflow_engine_v2_regressions_detected"
            ),
            "scenario_count": len(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
            "production_scenario_count": len(PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES),
            "persisted_run_count": len(persisted_runs),
            "run_count": active_contract["summary"]["run_count"],
            "lease_receipt_count": active_contract["summary"]["lease_receipt_count"],
            "blocked_lease_count": active_contract["summary"]["blocked_lease_count"],
            "transition_receipt_count": active_contract["summary"]["transition_receipt_count"],
            "blocked_transition_count": active_contract["summary"]["blocked_transition_count"],
            "trigger_receipt_count": active_contract["summary"]["trigger_receipt_count"],
            "deduped_trigger_count": active_contract["summary"]["deduped_trigger_count"],
            "blocked_recovery_count": active_contract["summary"]["blocked_recovery_count"],
            "blocked_artifact_adoption_count": active_contract["summary"]["blocked_artifact_adoption_count"],
            "claim_boundary": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
        },
        "scenario_names": list(DURABLE_WORKFLOW_ENGINE_V2_SCENARIO_NAMES),
        "production_scenario_names": list(PRODUCTION_DURABLE_ORCHESTRATION_SCENARIO_NAMES),
        "failure_report": failure_report,
        "policy": durable_workflow_v2_policy_payload(),
        "active_contract": active_contract,
        "proof_contract": proof_contract,
        "persisted_contract": persisted_contract,
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
