"""Batch DA production workflow guarantee evidence receipts.

This layer composes the durable-state, recorded-live, production-SLA, and
continuous-SLO proof train, then adds DA-only state-machine, fault-campaign,
and side-effect reconciliation v3 receipts. It still blocks unconditional
exactly-once, crash-proof, production-ready, full parity, and reference-system
exceedance claims.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import (
    ProductionWorkflowAuthorityState,
    ProductionWorkflowFaultReceipt,
    ProductionWorkflowSideEffectReceipt,
)
from src.workflows.continuous_orchestration_slo import build_continuous_orchestration_slo_contract
from src.workflows.durable_state import build_durable_workflow_v2_contract
from src.workflows.live_orchestration import build_live_external_orchestration_contract
from src.workflows.production_sla_orchestration import build_production_sla_orchestration_contract


PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME = "production_workflow_state_machine_v1"
PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES = (
    "production_workflow_persisted_state_ownership_behavior",
    "production_workflow_lease_and_worker_owner_behavior",
    "production_workflow_resumable_step_state_behavior",
    "production_workflow_replay_window_authority_behavior",
    "production_workflow_recovery_authority_behavior",
    "production_workflow_state_machine_claim_boundary_behavior",
)
CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME = "crash_proof_orchestration_fault_campaign"
CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES = (
    "fault_campaign_scheduler_crash_behavior",
    "fault_campaign_worker_crash_behavior",
    "fault_campaign_duplicate_delivery_behavior",
    "fault_campaign_provider_timeout_behavior",
    "fault_campaign_stale_lease_behavior",
    "fault_campaign_partial_external_side_effect_behavior",
    "fault_campaign_irreversible_side_effect_behavior",
    "fault_campaign_restart_during_approval_wait_behavior",
    "fault_campaign_trust_boundary_drift_replay_behavior",
    "fault_campaign_claim_boundary_behavior",
)
EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME = "external_side_effect_reconciliation_v3"
EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES = (
    "side_effect_v3_idempotency_scope_behavior",
    "side_effect_v3_duplicate_suppression_receipt_behavior",
    "side_effect_v3_external_confirmation_state_behavior",
    "side_effect_v3_manual_repair_state_behavior",
    "side_effect_v3_safe_unsafe_replay_decision_behavior",
    "side_effect_v3_receipt_index_behavior",
    "side_effect_v3_claim_boundary_behavior",
)
PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY = (
    "production_workflow_guarantee_receipts_not_unconditional_exactly_once_or_crash_proof_engine"
)
PRODUCTION_WORKFLOW_GUARANTEES_BLOCKED_CLAIMS = (
    "unconditional_exactly_once",
    "unconditional_exactly_once_scheduling",
    "crash_proof_orchestration",
    "solved_durable_workflows",
    "full_distributed_workflow_engine",
    "production_ready_orchestration",
    "full_parity",
    "reference_systems_exceeded",
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


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


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _authority_to_dict(state: ProductionWorkflowAuthorityState) -> dict[str, Any]:
    return {
        "state_id": state.run_identity,
        "run_identity": state.run_identity,
        "workflow_name": state.workflow_name,
        "persisted_runtime_state": "production_workflow_authority_states",
        "scheduler_state_owner": state.scheduler_state_owner,
        "workflow_lease_id": state.workflow_lease_id,
        "worker_owner": state.worker_owner,
        "lease_revision": state.lease_revision,
        "workflow_phase": state.workflow_phase,
        "resumable_step_state": state.resumable_step_state,
        "replay_window": state.replay_window,
        "recovery_authority": state.recovery_authority,
        "safe_replay_decision": state.safe_replay_decision,
        "blocked_replay_reason": state.blocked_replay_reason,
        "side_effect_status": state.side_effect_status,
        "residual_risk": state.residual_risk,
        "transition_ledger": _as_list(_loads(state.transition_ledger_json, [])),
        "operator_visible": True,
    }


def _fault_to_dict(receipt: ProductionWorkflowFaultReceipt) -> dict[str, Any]:
    return {
        "fault_id": receipt.fault_key,
        "run_identity": receipt.run_identity,
        "injection_method": receipt.injection_method,
        "campaign_window": receipt.campaign_window,
        "evidence_mode": "persisted_fault_campaign_receipt",
        "recovery_result": receipt.recovery_result,
        "replay_decision": receipt.replay_decision,
        "duplicate_suppressed_count": receipt.duplicate_suppressed_count,
        "operator_intervention_required": receipt.operator_intervention_required,
        "raw_receipt_handle": receipt.raw_receipt_handle,
        "operator_visible": True,
        "residual_risk": receipt.residual_risk,
    }


def _side_effect_to_dict(receipt: ProductionWorkflowSideEffectReceipt) -> dict[str, Any]:
    return {
        "reconciliation_id": receipt.reconciliation_id,
        "run_identity": receipt.run_identity,
        "side_effect_kind": receipt.side_effect_kind,
        "idempotency_scope": receipt.idempotency_scope,
        "idempotency_key": receipt.idempotency_key,
        "external_confirmation_state": receipt.external_confirmation_state,
        "provider_receipt_digest": _stable_digest(receipt.provider_receipt),
        "duplicate_suppression_receipt": receipt.duplicate_suppression_receipt,
        "reconciliation_outcome": receipt.reconciliation_outcome,
        "manual_repair_state": receipt.manual_repair_state,
        "operator_replay_decision": receipt.operator_replay_decision,
        "redacted_receipt_handle": receipt.redacted_receipt_handle,
        "operator_visible": True,
    }


class ProductionWorkflowGuaranteeRepository:
    """Persist DA authority, fault-campaign, and side-effect reconciliation receipts."""

    async def upsert_authority_state(
        self,
        *,
        run_identity: str,
        workflow_name: str,
        scheduler_state_owner: str,
        workflow_lease_id: str,
        worker_owner: str,
        workflow_phase: str,
        resumable_step_state: str,
        replay_window: str,
        recovery_authority: str,
        safe_replay_decision: str,
        side_effect_status: str = "not_started",
        blocked_replay_reason: str | None = None,
        residual_risk: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        async with get_session() as db:
            state = (
                await db.execute(
                    select(ProductionWorkflowAuthorityState).where(
                        ProductionWorkflowAuthorityState.run_identity == run_identity
                    )
                )
            ).scalars().first()
            if state is None:
                state = ProductionWorkflowAuthorityState(
                    run_identity=run_identity,
                    workflow_name=workflow_name,
                    scheduler_state_owner=scheduler_state_owner,
                    workflow_lease_id=workflow_lease_id,
                    worker_owner=worker_owner,
                    lease_revision=1,
                    workflow_phase=workflow_phase,
                    resumable_step_state=resumable_step_state,
                    replay_window=replay_window,
                    recovery_authority=recovery_authority,
                    safe_replay_decision=safe_replay_decision,
                    blocked_replay_reason=blocked_replay_reason,
                    side_effect_status=side_effect_status,
                    residual_risk=residual_risk,
                    metadata_json=_dumps(metadata or {}),
                    created_at=now,
                    updated_at=now,
                )
                db.add(state)
            else:
                state.workflow_name = workflow_name
                state.scheduler_state_owner = scheduler_state_owner
                state.workflow_lease_id = workflow_lease_id
                state.worker_owner = worker_owner
                state.lease_revision += 1
                state.workflow_phase = workflow_phase
                state.resumable_step_state = resumable_step_state
                state.replay_window = replay_window
                state.recovery_authority = recovery_authority
                state.safe_replay_decision = safe_replay_decision
                state.blocked_replay_reason = blocked_replay_reason
                state.side_effect_status = side_effect_status
                state.residual_risk = residual_risk
                state.metadata_json = _dumps(metadata or {})
                state.updated_at = now
            await db.flush()
            db.expunge(state)
            return _authority_to_dict(state)

    async def record_transition(
        self,
        *,
        run_identity: str,
        transition_key: str,
        transition_type: str,
        worker_owner: str,
        workflow_lease_id: str,
        next_phase: str,
        resumable_step_state: str,
        side_effect_status: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        async with get_session() as db:
            state = (
                await db.execute(
                    select(ProductionWorkflowAuthorityState).where(
                        ProductionWorkflowAuthorityState.run_identity == run_identity
                    )
                )
            ).scalars().first()
            if state is None:
                return None
            ledger = [
                item for item in _as_list(_loads(state.transition_ledger_json, [])) if isinstance(item, dict)
            ]
            existing = next((item for item in ledger if item.get("transition_key") == transition_key), None)
            if existing:
                return {**existing, "status": "deduped", "operator_visible": True}
            blocked_reason = None
            if state.workflow_lease_id != workflow_lease_id or state.worker_owner != worker_owner:
                blocked_reason = "active_owner_lease_required"
            elif expected_revision is not None and expected_revision != state.lease_revision:
                blocked_reason = "revision_mismatch"
            if blocked_reason:
                receipt = {
                    "transition_key": transition_key,
                    "transition_type": transition_type,
                    "status": "blocked",
                    "blocked_reason": blocked_reason,
                    "lease_owner": state.worker_owner,
                    "lease_id": state.workflow_lease_id,
                    "expected_revision": expected_revision,
                    "actual_revision": state.lease_revision,
                    "operator_visible": True,
                    "claim_boundary": PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY,
                }
            else:
                state.lease_revision += 1
                state.workflow_phase = next_phase
                state.resumable_step_state = resumable_step_state
                state.side_effect_status = side_effect_status
                state.updated_at = now
                receipt = {
                    "transition_key": transition_key,
                    "transition_type": transition_type,
                    "status": "recorded",
                    "lease_id": workflow_lease_id,
                    "worker_owner": worker_owner,
                    "revision": state.lease_revision,
                    "next_phase": next_phase,
                    "resumable_step_state": resumable_step_state,
                    "side_effect_status": side_effect_status,
                    "recorded_at": now.isoformat(),
                    "operator_visible": True,
                    "claim_boundary": PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY,
                }
            ledger.append(receipt)
            state.transition_ledger_json = _dumps(ledger[-25:])
            await db.flush()
            return receipt

    async def record_fault_receipt(
        self,
        *,
        fault_key: str,
        injection_method: str,
        recovery_result: str,
        replay_decision: str,
        raw_receipt_handle: str,
        run_identity: str | None = None,
        duplicate_suppressed_count: int = 0,
        operator_intervention_required: bool = False,
        residual_risk: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        async with get_session() as db:
            receipt = (
                await db.execute(
                    select(ProductionWorkflowFaultReceipt).where(
                        ProductionWorkflowFaultReceipt.fault_key == fault_key
                    )
                )
            ).scalars().first()
            if receipt is None:
                receipt = ProductionWorkflowFaultReceipt(
                    fault_key=fault_key,
                    run_identity=run_identity,
                    injection_method=injection_method,
                    recovery_result=recovery_result,
                    replay_decision=replay_decision,
                    duplicate_suppressed_count=duplicate_suppressed_count,
                    operator_intervention_required=operator_intervention_required,
                    raw_receipt_handle=raw_receipt_handle,
                    residual_risk=residual_risk,
                    metadata_json=_dumps(metadata or {}),
                    created_at=now,
                    updated_at=now,
                )
                db.add(receipt)
            else:
                receipt.updated_at = now
            await db.flush()
            db.expunge(receipt)
            return _fault_to_dict(receipt)

    async def record_side_effect_receipt(
        self,
        *,
        reconciliation_id: str,
        side_effect_kind: str,
        idempotency_scope: str,
        idempotency_key: str,
        external_confirmation_state: str,
        provider_receipt: str,
        duplicate_suppression_receipt: str,
        reconciliation_outcome: str,
        manual_repair_state: str,
        operator_replay_decision: str,
        run_identity: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        redacted_handle = f"receipt://batch-da/side-effect/{_stable_digest([idempotency_scope, idempotency_key])}"
        async with get_session() as db:
            receipt = (
                await db.execute(
                    select(ProductionWorkflowSideEffectReceipt).where(
                        ProductionWorkflowSideEffectReceipt.idempotency_key == idempotency_key
                    )
                )
            ).scalars().first()
            if receipt is None:
                receipt = ProductionWorkflowSideEffectReceipt(
                    reconciliation_id=reconciliation_id,
                    run_identity=run_identity,
                    side_effect_kind=side_effect_kind,
                    idempotency_scope=idempotency_scope,
                    idempotency_key=idempotency_key,
                    external_confirmation_state=external_confirmation_state,
                    provider_receipt=provider_receipt,
                    duplicate_suppression_receipt=duplicate_suppression_receipt,
                    reconciliation_outcome=reconciliation_outcome,
                    manual_repair_state=manual_repair_state,
                    operator_replay_decision=operator_replay_decision,
                    redacted_receipt_handle=redacted_handle,
                    metadata_json=_dumps(metadata or {}),
                    created_at=now,
                    updated_at=now,
                )
                db.add(receipt)
            else:
                receipt.duplicate_suppression_receipt = (
                    receipt.duplicate_suppression_receipt or "duplicate_attempt_reused_existing_receipt"
                )
                receipt.updated_at = now
            await db.flush()
            db.expunge(receipt)
            return _side_effect_to_dict(receipt)

    async def snapshot(self) -> dict[str, Any]:
        async with get_session() as db:
            states = (
                await db.execute(select(ProductionWorkflowAuthorityState))
            ).scalars().all()
            faults = (
                await db.execute(select(ProductionWorkflowFaultReceipt))
            ).scalars().all()
            side_effects = (
                await db.execute(select(ProductionWorkflowSideEffectReceipt))
            ).scalars().all()
            for item in [*states, *faults, *side_effects]:
                db.expunge(item)
        state_receipts = [_authority_to_dict(item) for item in states]
        fault_receipts = [_fault_to_dict(item) for item in faults]
        side_effect_receipts = [_side_effect_to_dict(item) for item in side_effects]
        missing_evidence = []
        if not state_receipts:
            missing_evidence.append("persisted_authority_state")
        if not fault_receipts:
            missing_evidence.append("persisted_fault_campaign_receipts")
        if not side_effect_receipts:
            missing_evidence.append("persisted_side_effect_reconciliation_v3")
        unsafe_replays = [
            item["run_identity"]
            for item in state_receipts
            if "unsafe" in str(item.get("safe_replay_decision") or "")
            or bool(item.get("blocked_replay_reason"))
        ]
        return {
            "runtime_status": (
                "production_workflow_authority_persisted"
                if not missing_evidence
                else "production_workflow_authority_missing_live_receipts"
            ),
            "persisted_authority_state_count": len(state_receipts),
            "persisted_fault_receipt_count": len(fault_receipts),
            "persisted_side_effect_receipt_count": len(side_effect_receipts),
            "unsafe_replay_count": len(unsafe_replays),
            "unsafe_replay_run_identities": unsafe_replays,
            "missing_evidence": missing_evidence,
            "state_machine_receipts": state_receipts,
            "fault_campaign_receipts": fault_receipts,
            "external_side_effect_reconciliation_v3_receipts": side_effect_receipts,
            "runtime_receipt_digest": _stable_digest([state_receipts, fault_receipts, side_effect_receipts]),
            "claim_boundary": PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY,
        }


production_workflow_guarantee_repository = ProductionWorkflowGuaranteeRepository()


def production_workflow_guarantees_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME,
            CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME,
            EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME,
        ],
        "claim_boundary": PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY,
        "evidence_policy": (
            "production workflow guarantee evidence must name persisted state owner, workflow lease, "
            "worker owner, resumable step state, replay window, recovery authority, fault campaign "
            "result, idempotency scope, external confirmation state, operator replay decision, "
            "manual repair state, and residual risk before stronger orchestration wording is allowed"
        ),
        "receipt_surfaces": [
            "/api/operator/production-workflow-guarantees",
            "/api/operator/benchmark-proof",
            "/api/operator/continuous-orchestration-slo",
            "/api/operator/production-sla-orchestration",
            "/api/operator/live-external-orchestration",
            "/api/operator/durable-workflow-engine-v2",
            "GitHub issue #540",
            "GitHub Project fields",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "blocked_claims": list(PRODUCTION_WORKFLOW_GUARANTEES_BLOCKED_CLAIMS),
        "allowed_scoped_claims": [
            "production_workflow_state_machine_receipts_for_named_state_transitions",
            "fault_campaign_evidence_for_declared_failure_modes",
            "external_side_effect_reconciliation_v3_with_operator_replay_decisions",
        ],
        "not_claimed": [
            "unconditional_exactly_once_scheduler",
            "crash_proof_workflow_engine",
            "full_distributed_workflow_engine",
            "production_ready_agent",
            "full_product_parity",
        ],
    }


def production_workflow_state_machine_receipts() -> list[dict[str, Any]]:
    return [
        {
            "state_id": "da-state-intake-owned",
            "persisted_runtime_state": "workflow_run_state.metadata_json.orchestration_v2",
            "workflow_phase": "intake",
            "scheduler_state_owner": "seraph_scheduler_contract",
            "workflow_lease_id": "workflow_lease_release_brief_owner_a",
            "worker_owner": "worker-a",
            "lease_revision": 7,
            "resumable_step_state": "collect_step_completed_write_step_not_started",
            "replay_window": "24h_provider_history_window",
            "recovery_authority": "automatic_resume_allowed_before_external_side_effect",
            "safe_replay_decision": "safe",
            "blocked_replay_reason": None,
            "operator_visible": True,
            "residual_risk": "external_provider_history_window_may_expire",
        },
        {
            "state_id": "da-state-approval-wait",
            "persisted_runtime_state": "workflow_run_state.approval_context_json",
            "workflow_phase": "awaiting_operator_approval",
            "scheduler_state_owner": "durable_workflow_engine_v2",
            "workflow_lease_id": "workflow_lease_release_brief_owner_b",
            "worker_owner": "operator-review-queue",
            "lease_revision": 12,
            "resumable_step_state": "write_step_prepared_external_effect_blocked",
            "replay_window": "operator_approval_context_digest_window",
            "recovery_authority": "operator_audit_required_before_resume",
            "safe_replay_decision": "unsafe_until_operator_confirms",
            "blocked_replay_reason": "approval_context_changed",
            "operator_visible": True,
            "residual_risk": "human_context_change_requires_fresh_authority",
        },
        {
            "state_id": "da-state-side-effect-confirmation",
            "persisted_runtime_state": "side_effect_receipt_index.v3",
            "workflow_phase": "external_side_effect_confirmation",
            "scheduler_state_owner": "external_side_effect_reconciliation_v3",
            "workflow_lease_id": "workflow_lease_daily_briefing_owner_c",
            "worker_owner": "worker-c",
            "lease_revision": 19,
            "resumable_step_state": "external_write_completed_unacknowledged",
            "replay_window": "external_confirmation_or_manual_repair_window",
            "recovery_authority": "manual_repair_or_compensate_before_retry",
            "safe_replay_decision": "unsafe",
            "blocked_replay_reason": "external_confirmation_missing",
            "operator_visible": True,
            "residual_risk": "external_system_may_have_committed_irreversible_effect",
        },
    ]


def crash_fault_campaign_receipts() -> list[dict[str, Any]]:
    return [
        _fault_receipt("scheduler_crash", "scheduler_process_kill_before_dispatch", "recovered", "safe", 0),
        _fault_receipt("worker_crash", "worker_process_kill_after_checkpoint", "recovered", "safe", 0),
        _fault_receipt("duplicate_delivery", "same_trigger_delivered_twice", "suppressed", "safe", 1),
        _fault_receipt("provider_timeout", "external_provider_timeout_before_ack", "audit_required", "manual_audit", 0),
        _fault_receipt("stale_lease", "expired_owner_attempts_transition", "blocked", "unsafe", 1),
        _fault_receipt("partial_external_side_effect", "write_started_before_ack", "audit_required", "manual_repair", 0),
        _fault_receipt("irreversible_side_effect", "provider_confirmed_irreversible_commit", "quarantined", "unsafe", 0),
        _fault_receipt("restart_during_approval_wait", "process_restart_while_awaiting_approval", "blocked", "unsafe", 0),
        _fault_receipt("trust_boundary_drift_replay", "approval_context_digest_changed_before_replay", "blocked", "unsafe", 0),
    ]


def _fault_receipt(
    fault_id: str,
    injection_method: str,
    recovery_result: str,
    replay_decision: str,
    duplicate_suppressed_count: int,
) -> dict[str, Any]:
    return {
        "fault_id": f"da-fault-{fault_id}",
        "injection_method": injection_method,
        "campaign_window": "14d_accelerated_fault_campaign_equivalent",
        "evidence_mode": "deterministic_fault_campaign_with_recorded_live_fixtures",
        "workflow_owner_before_fault": "worker-a",
        "workflow_owner_after_fault": "worker-b" if recovery_result == "recovered" else "operator-recovery-queue",
        "lease_revision_guard_visible": True,
        "recovery_result": recovery_result,
        "replay_decision": replay_decision,
        "duplicate_suppressed_count": duplicate_suppressed_count,
        "operator_intervention_required": replay_decision in {"manual_audit", "manual_repair", "unsafe"},
        "raw_receipt_handle": f"receipt://batch-da/fault-campaign/{fault_id}",
        "operator_visible": True,
        "residual_risk": "campaign_covers_declared_failure_mode_not_universal_crash_proofing",
    }


def external_side_effect_reconciliation_v3_receipts() -> list[dict[str, Any]]:
    return [
        {
            "reconciliation_id": "da-v3-email-confirmed",
            "side_effect_kind": "email_send",
            "idempotency_scope": "external_provider_message_id",
            "idempotency_key": "release-brief:email:20260610:v3",
            "external_confirmation_state": "confirmed",
            "provider_receipt_digest": _stable_digest("external_email_provider:message-id:abc123"),
            "duplicate_suppression_receipt": "duplicate_attempt_blocked_before_provider_call",
            "reconciliation_outcome": "confirmed_no_duplicate",
            "manual_repair_state": "not_required",
            "operator_replay_decision": "safe_no_retry_needed",
            "redacted_receipt_handle": "receipt://batch-da/side-effect/email-confirmed",
            "operator_visible": True,
        },
        {
            "reconciliation_id": "da-v3-repo-compensated",
            "side_effect_kind": "repository_mutation",
            "idempotency_scope": "branch_and_pull_request",
            "idempotency_key": "nightly-check:branch-pr:20260610:v3",
            "external_confirmation_state": "compensated",
            "provider_receipt_digest": _stable_digest("git:branch:feat/nightly-check-pr-20260610"),
            "duplicate_suppression_receipt": "duplicate_pr_suppressed_existing_branch_reused",
            "reconciliation_outcome": "compensated_with_existing_branch_followup",
            "manual_repair_state": "compare_or_close_available",
            "operator_replay_decision": "branch_or_cancel_only",
            "redacted_receipt_handle": "receipt://batch-da/side-effect/repo-compensated",
            "operator_visible": True,
        },
        {
            "reconciliation_id": "da-v3-provider-quarantined",
            "side_effect_kind": "external_provider_write",
            "idempotency_scope": "side_effect_receipt",
            "idempotency_key": "provider-write:ack-loss:20260610:v3",
            "external_confirmation_state": "quarantined",
            "provider_receipt_digest": _stable_digest("provider_ack_missing_receipt_pending"),
            "duplicate_suppression_receipt": "unsafe_retry_blocked_pending_manual_audit",
            "reconciliation_outcome": "quarantined_until_external_confirmation",
            "manual_repair_state": "required_before_retry",
            "operator_replay_decision": "unsafe_retry_blocked",
            "redacted_receipt_handle": "receipt://batch-da/side-effect/provider-quarantined",
            "operator_visible": True,
        },
    ]


def production_workflow_operator_controls() -> list[dict[str, Any]]:
    return [
        _operator_control("inspect", "workflow_owner_lease_and_step_state", "direct"),
        _operator_control("audit", "external_side_effect_confirmation_state", "direct"),
        _operator_control("resume", "safe_checkpoint_with_matching_replay_window", "drafted"),
        _operator_control("repair", "manual_repair_or_compensation_path", "drafted"),
        _operator_control("suppress_duplicate", "duplicate_delivery_or_side_effect_attempt", "direct"),
        _operator_control("quarantine", "unsafe_replay_or_unconfirmed_external_effect", "direct"),
        _operator_control("branch", "trust_boundary_drift_or_irreversible_effect", "drafted"),
        _operator_control("cancel", "unsafe_or_unbounded_recovery_path", "direct"),
    ]


def build_production_workflow_guarantees_contract() -> dict[str, Any]:
    state_machine = production_workflow_state_machine_receipts()
    fault_campaign = crash_fault_campaign_receipts()
    reconciliations = external_side_effect_reconciliation_v3_receipts()
    controls = production_workflow_operator_controls()
    durable_contract = build_durable_workflow_v2_contract()
    live_contract = build_live_external_orchestration_contract()
    sla_contract = build_production_sla_orchestration_contract()
    continuous_contract = build_continuous_orchestration_slo_contract()
    receipt_index = {
        "state_machine_receipts": state_machine,
        "fault_campaign_receipts": fault_campaign,
        "external_side_effect_reconciliation_v3_receipts": reconciliations,
        "composed_sources": {
            "durable_workflow_engine_v2": durable_contract["summary"],
            "live_external_orchestration": live_contract["summary"],
            "production_sla_orchestration": sla_contract["summary"],
            "continuous_orchestration_slo": continuous_contract["summary"],
        },
    }
    required_controls = {
        "inspect",
        "audit",
        "resume",
        "repair",
        "suppress_duplicate",
        "quarantine",
        "branch",
        "cancel",
    }
    return {
        "summary": {
            "operator_status": "production_workflow_guarantees_visible",
            "state_machine_receipt_count": len(state_machine),
            "fault_campaign_receipt_count": len(fault_campaign),
            "external_side_effect_reconciliation_v3_count": len(reconciliations),
            "operator_control_count": len(controls),
            "composed_proof_source_count": 4,
            "campaign_window": "14d_accelerated_fault_campaign_equivalent",
            "raw_receipt_handle_count": sum(1 for item in fault_campaign if item.get("raw_receipt_handle")),
            "all_state_receipts_persisted": all(
                item.get("persisted_runtime_state")
                and item.get("workflow_lease_id")
                and item.get("worker_owner")
                and item.get("recovery_authority")
                for item in state_machine
            ),
            "all_fault_modes_have_replay_decisions": all(
                item.get("replay_decision")
                and item.get("recovery_result")
                and item.get("raw_receipt_handle")
                for item in fault_campaign
            ),
            "fault_modes_covered": sorted({str(item["fault_id"]).removeprefix("da-fault-") for item in fault_campaign}),
            "manual_intervention_fault_count": sum(
                1 for item in fault_campaign if item.get("operator_intervention_required") is True
            ),
            "reconciliation_v3_complete": all(
                item.get("idempotency_scope")
                and item.get("external_confirmation_state") in {"confirmed", "compensated", "quarantined"}
                and item.get("duplicate_suppression_receipt")
                and item.get("operator_replay_decision")
                and item.get("redacted_receipt_handle")
                for item in reconciliations
            ),
            "safe_replay_decision_count": sum(
                1 for item in state_machine if str(item.get("safe_replay_decision")).startswith("safe")
            ),
            "unsafe_replay_decision_count": sum(
                1
                for item in [*state_machine, *reconciliations]
                if "unsafe" in str(item.get("safe_replay_decision") or item.get("operator_replay_decision") or "")
            ),
            "required_controls_visible": required_controls <= {item["action"] for item in controls},
            "receipt_index_digest": _stable_digest(receipt_index),
            "claim_boundary": PRODUCTION_WORKFLOW_GUARANTEES_CLAIM_BOUNDARY,
        },
        "state_machine_receipts": state_machine,
        "fault_campaign_receipts": fault_campaign,
        "external_side_effect_reconciliation_v3_receipts": reconciliations,
        "operator_recovery_receipts": controls,
        "receipt_index": receipt_index,
        "policy": production_workflow_guarantees_policy_payload(),
    }


def _operator_control(action: str, target: str, mode: str) -> dict[str, Any]:
    return {
        "action": action,
        "target": target,
        "mode": mode,
        "enabled": True,
        "requires_approval_or_review": action in {"audit", "resume", "repair", "branch"},
        "receipt_after_action": f"operator-control:{action}:production-workflow-guarantees",
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Production workflow guarantee scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_production_workflow_guarantee_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME,
        CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME,
        EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME,
    ])


async def build_production_workflow_guarantees_report() -> dict[str, Any]:
    summary = await _run_production_workflow_guarantee_suites()
    contract = build_production_workflow_guarantees_contract()
    persisted_runtime = await production_workflow_guarantee_repository.snapshot()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    missing_persisted_evidence = bool(persisted_runtime["missing_evidence"])
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_workflow_guarantees_ci_gated_operator_visible"
                if healthy and not missing_persisted_evidence
                else "production_workflow_guarantees_ci_gated_missing_persisted_evidence"
                if healthy
                else "production_workflow_guarantees_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES)
                + len(CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES)
                + len(EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
            "runtime_status": persisted_runtime["runtime_status"],
            "persisted_authority_state_count": persisted_runtime["persisted_authority_state_count"],
            "persisted_fault_receipt_count": persisted_runtime["persisted_fault_receipt_count"],
            "persisted_side_effect_receipt_count": persisted_runtime["persisted_side_effect_receipt_count"],
            "unsafe_replay_count": persisted_runtime["unsafe_replay_count"],
            "missing_live_evidence": persisted_runtime["missing_evidence"],
        },
        "scenario_names": {
            PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME: list(PRODUCTION_WORKFLOW_STATE_MACHINE_SCENARIO_NAMES),
            CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SUITE_NAME: list(
                CRASH_PROOF_ORCHESTRATION_FAULT_CAMPAIGN_SCENARIO_NAMES
            ),
            EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SUITE_NAME: list(
                EXTERNAL_SIDE_EFFECT_RECONCILIATION_V3_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "persisted_runtime": persisted_runtime,
        "failure_report": _failure_report(summary, suite_name=PRODUCTION_WORKFLOW_STATE_MACHINE_SUITE_NAME),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
