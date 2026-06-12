"""Batch DQ post-DP durable orchestration gap-closure receipts.

This module connects persisted durable workflow v2 facts to the post-DP DQ
operator proof surface. It is not a new workflow engine and does not lift
exactly-once, crash-proof, production-ready, full-parity, or reference-system
exceedance claims.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.workflows.durable_state import (
    DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
    _as_dict,
    _as_list,
    _workflow_v2_metadata,
    build_durable_workflow_v2_contract,
    durable_workflow_v2_policy_payload,
    workflow_state_repository,
)


POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME = "post_dp_durable_orchestration_v1"
POST_DP_DURABLE_ORCHESTRATION_SCENARIO_NAMES = (
    "post_dp_persisted_recovery_packet_behavior",
    "post_dp_restart_preserves_orchestration_metadata_behavior",
    "post_dp_lease_guarded_recovery_authority_behavior",
    "post_dp_operator_receipt_redaction_behavior",
    "post_dp_claim_boundary_behavior",
)
MULTI_AGENT_HANDOFF_RECOVERY_SUITE_NAME = "multi_agent_handoff_recovery_v1"
MULTI_AGENT_HANDOFF_RECOVERY_SCENARIO_NAMES = (
    "handoff_receiver_authority_acceptance_behavior",
    "handoff_pending_approval_fail_closed_behavior",
    "handoff_revision_guard_behavior",
)
SCHEDULER_CRASH_RESTART_RECOVERY_SUITE_NAME = "scheduler_crash_restart_recovery_v1"
SCHEDULER_CRASH_RESTART_RECOVERY_SCENARIO_NAMES = (
    "scheduler_restart_trigger_record_only_behavior",
    "scheduler_duplicate_trigger_no_external_action_behavior",
    "scheduler_stale_heartbeat_recovery_packet_behavior",
)
SIDE_EFFECT_RECONCILIATION_V5_SUITE_NAME = "side_effect_reconciliation_v5"
SIDE_EFFECT_RECONCILIATION_V5_SCENARIO_NAMES = (
    "side_effect_v5_idempotency_digest_behavior",
    "side_effect_v5_duplicate_owner_replay_block_behavior",
    "side_effect_v5_manual_repair_after_unknown_ack_behavior",
)
ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SUITE_NAME = "orchestration_false_claim_scan_v2"
ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES = (
    "orchestration_false_claim_v2_blocks_exactly_once",
    "orchestration_false_claim_v2_blocks_crash_proof",
    "orchestration_false_claim_v2_blocks_full_parity",
)

POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY = (
    "post_dp_durable_orchestration_gap_closure_not_solved_workflow_or_exactly_once"
)
POST_DP_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS = (
    "unconditional_exactly_once_scheduling",
    "crash_proof_orchestration",
    "solved_durable_workflows",
    "langgraph_class_workflow_parity",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
    "superiority",
)
_DQ_PERSISTED_RECEIPT_KEYS = (
    "restart_recovery_receipts",
    "handoff_receipts",
    "guardian_recovery_receipts",
    "side_effect_boundary_receipts",
    "unsafe_recovery_refusal_receipts",
)


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def post_dp_durable_orchestration_policy_payload() -> dict[str, Any]:
    return {
        "claim_boundary": POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
        "builds_on": DURABLE_WORKFLOW_ENGINE_V2_CLAIM_BOUNDARY,
        "blocked_claims": list(POST_DP_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS),
        "operator_surfaces": [
            "/api/operator/post-dp-durable-orchestration",
            "/api/operator/durable-workflow-engine-v2",
            "/api/operator/production-orchestration-hard-guarantees",
            "/api/operator/benchmark-proof",
        ],
        "recovery_policy": (
            "recovery may continue only after durable state, lease ownership, replay window, "
            "side-effect reconciliation, and operator authority are visible"
        ),
        "guardian_policy": (
            "guardian context may add restraint or require audit; it never expands approval, "
            "credential, side-effect, replay, lease, or handoff authority"
        ),
        "redaction_policy": (
            "operator receipts expose digests and handles instead of raw checkpoint, result, "
            "provider, memory, artifact, or secret-ref payloads"
        ),
        "not_claimed": [
            "exactly_once_scheduler",
            "crash_proof_orchestration",
            "solved_durable_workflows",
            "langgraph_class_workflow_parity",
            "production_ready_product",
            "full_parity_or_reference_system_exceedance",
        ],
    }


def _fixture_runs() -> list[dict[str, Any]]:
    return [
        {
            "run_identity": "dq_release_long_work",
            "root_run_identity": "dq_release_long_work",
            "workflow_name": "release-hardening-long-work",
            "status": "interrupted",
            "last_completed_step_id": "review",
            "approval_context": {
                "risk_level": "medium",
                "execution_boundaries": ["workspace_filesystem", "delegation"],
                "delegated_specialists": ["critic"],
            },
            "metadata": {
                "orchestration_v2": {
                    "revision": 6,
                    "lease": {
                        "owner": "worker-a",
                        "lease_id": "dq-lease-worker-a",
                        "expires_at": "2026-06-12T08:10:00+00:00",
                    },
                    "restart_recovery_receipts": [
                        {
                            "kind": "restart_recovery",
                            "status": "interrupted",
                            "preserved_orchestration_v2": True,
                            "resume_receipt": True,
                        }
                    ],
                    "handoff_receipts": [
                        {
                            "kind": "handoff",
                            "from_owner": "worker-a",
                            "to_owner": "worker-b",
                            "status": "blocked",
                            "blocked_reason": "receiver_authority_not_accepted",
                            "operator_visible": True,
                        }
                    ],
                    "transition_ledger": [
                        {
                            "transition_key": "dq:resume:review",
                            "transition_type": "resume",
                            "owner": "worker-a",
                            "status": "recorded",
                            "idempotency_key": "dq:resume:review",
                        }
                    ],
                    "transition_block_receipts": [
                        {
                            "transition_key": "dq:resume:review",
                            "owner": "worker-b",
                            "status": "blocked",
                            "blocked_reason": "active_owner_lease_required",
                            "operator_visible": True,
                        }
                    ],
                    "trigger_ledger": [
                        {
                            "trigger_key": "dq:scheduler:restart",
                            "trigger_kind": "scheduler_restart",
                            "status": "recorded",
                            "external_action_allowed": False,
                            "authority_required": "recovery_plan_or_operator_resume_required_before_external_action",
                        },
                        {
                            "trigger_key": "dq:scheduler:restart",
                            "trigger_kind": "scheduler_restart",
                            "status": "deduped",
                            "external_action_allowed": False,
                        },
                    ],
                    "side_effect_boundary_receipts": [
                        {
                            "side_effect_kind": "repository_mutation",
                            "idempotency_scope": "repo_branch_commit_tree",
                            "idempotency_key_digest": "digest-repo-mutation",
                            "external_confirmation_state": "unknown_ack",
                            "reconciliation_status": "manual_repair_required",
                            "duplicate_suppressed": True,
                            "redacted_receipt_handle": "receipt://dq/side-effect/repo-mutation",
                        }
                    ],
                    "recovery_receipts": [
                        {
                            "kind": "recovery_plan",
                            "status": "ready",
                            "resume_from_step": "review",
                            "operator_visible": True,
                        }
                    ],
                    "guardian_recovery_receipts": [
                        {
                            "guardian_recovery_context_digest": "guardian-digest-release",
                            "restraint_posture": "operator_audit_required",
                            "reason_codes": ["recent_context_shift"],
                            "authority_expanded": False,
                        }
                    ],
                }
            },
        },
        {
            "run_identity": "dq_private_source_blocked",
            "root_run_identity": "dq_private_source_blocked",
            "workflow_name": "private-source-brief",
            "status": "failed",
            "last_completed_step_id": "fetch",
            "approval_context": {
                "risk_level": "high",
                "execution_boundaries": ["authenticated_external_source", "secret_read"],
                "accepts_secret_refs": True,
            },
            "metadata": {
                "orchestration_v2": {
                    "revision": 3,
                    "lease": {
                        "owner": "worker-c",
                        "lease_id": "dq-lease-worker-c",
                        "expires_at": "2026-06-12T08:20:00+00:00",
                    },
                    "recovery_receipts": [
                        {
                            "kind": "recovery_plan",
                            "status": "blocked",
                            "blocked_reason": "approval_context_changed",
                            "requires_fresh_run": True,
                            "operator_visible": True,
                        }
                    ],
                    "unsafe_recovery_refusal_receipts": [
                        {
                            "kind": "unsafe_recovery_refusal",
                            "blocked_reason": "approval_context_changed",
                            "requires_fresh_run": True,
                            "operator_visible": True,
                        }
                    ],
                    "guardian_recovery_receipts": [
                        {
                            "guardian_recovery_context_digest": "guardian-digest-private",
                            "restraint_posture": "fresh_approval_required",
                            "reason_codes": ["secret_scope_changed", "memory_context_stale"],
                            "authority_expanded": False,
                        }
                    ],
                }
            },
        },
    ]


def _redacted_recovery_packet(run: dict[str, Any]) -> dict[str, Any]:
    metadata = _workflow_v2_metadata(_as_dict(run.get("metadata")))
    v2 = _as_dict(metadata.get("orchestration_v2"))
    lease = _as_dict(v2.get("lease"))
    recovery = _as_dict((_as_list(v2.get("recovery_receipts")) or [{}])[-1])
    side_effect = _as_dict((_as_list(v2.get("side_effect_boundary_receipts")) or [{}])[-1])
    guardian = _as_dict((_as_list(v2.get("guardian_recovery_receipts")) or [{}])[-1])
    triggers = [_as_dict(item) for item in _as_list(v2.get("trigger_ledger")) if isinstance(item, dict)]
    transition_blocks = [
        _as_dict(item)
        for item in _as_list(v2.get("transition_block_receipts"))
        if isinstance(item, dict)
    ]
    restart_receipts = [
        _as_dict(item)
        for item in _as_list(v2.get("restart_recovery_receipts"))
        if isinstance(item, dict)
    ]
    handoffs = [
        _as_dict(item)
        for item in _as_list(v2.get("handoff_receipts"))
        if isinstance(item, dict)
    ]
    unsafe_refusals = [
        _as_dict(item)
        for item in _as_list(v2.get("unsafe_recovery_refusal_receipts"))
        if isinstance(item, dict)
    ]
    approval_digest = _digest(_as_dict(run.get("approval_context")))
    run_identity_digest = _digest(run.get("run_identity"))
    workflow_name_digest = _digest(run.get("workflow_name"))
    lease_id_digest = lease.get("lease_id_digest") or _digest(lease.get("lease_id"))
    lease_owner_digest = _digest(lease.get("owner"))
    lease_expires_at = lease.get("expires_at")
    return {
        "run_handle": f"workflow-run:{run_identity_digest}",
        "run_identity_digest": run_identity_digest,
        "workflow_name_digest": workflow_name_digest,
        "status": run.get("status"),
        "state_hash": _digest({
            "run_identity": run.get("run_identity"),
            "status": run.get("status"),
            "revision": v2.get("revision"),
            "lease": lease,
            "recovery": recovery,
        }),
        "approval_context_digest": approval_digest,
        "lease_owner_digest": lease_owner_digest,
        "lease_id_digest": lease_id_digest,
        "revision": int(v2.get("revision") or 0),
        "recovery_status": recovery.get("status") or "missing",
        "recovery_authority": (
            "operator_visible_resume"
            if recovery.get("status") == "ready"
            else "operator_or_fresh_approval_required"
        ),
        "replay_window": {
            "lease_expires_at": lease_expires_at,
            "external_action_allowed": False,
            "authority_required": (
                "lease_owner_and_recovery_plan_required_before_replay"
                if recovery.get("status") == "ready"
                else "fresh_operator_authority_required_before_replay"
            ),
            "idempotency_scope": side_effect.get("idempotency_scope"),
            "idempotency_key_digest": side_effect.get("idempotency_key_digest"),
        },
        "restart_preserved_metadata": any(
            item.get("preserved_orchestration_v2") is True for item in restart_receipts
        ),
        "handoff_block_count": sum(1 for item in handoffs if item.get("status") == "blocked"),
        "transition_block_count": len(transition_blocks),
        "trigger_external_action_allowed_count": sum(
            1 for item in triggers if item.get("external_action_allowed") is True
        ),
        "deduped_trigger_count": sum(1 for item in triggers if item.get("status") == "deduped"),
        "side_effect_boundary": {
            "side_effect_kind": side_effect.get("side_effect_kind"),
            "idempotency_scope": side_effect.get("idempotency_scope"),
            "idempotency_key_digest": side_effect.get("idempotency_key_digest"),
            "external_confirmation_state": side_effect.get("external_confirmation_state"),
            "reconciliation_status": side_effect.get("reconciliation_status"),
            "duplicate_suppressed": bool(side_effect.get("duplicate_suppressed")),
            "redacted_receipt_handle": side_effect.get("redacted_receipt_handle"),
        },
        "guardian_recovery": {
            "guardian_recovery_context_digest": guardian.get("guardian_recovery_context_digest"),
            "restraint_posture": guardian.get("restraint_posture"),
            "reason_codes": _as_list(guardian.get("reason_codes")),
            "authority_expanded": bool(guardian.get("authority_expanded", False)),
        },
        "unsafe_recovery_refusal_count": len(unsafe_refusals),
        "raw_payloads_redacted": True,
        "blocked_claims": list(POST_DP_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS),
        "claim_boundary": POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
    }


def _has_dq_persisted_evidence(run: dict[str, Any]) -> bool:
    metadata = _workflow_v2_metadata(_as_dict(run.get("metadata")))
    v2 = _as_dict(metadata.get("orchestration_v2"))
    return any(_as_list(v2.get(key)) for key in _DQ_PERSISTED_RECEIPT_KEYS)


def _empty_redacted_durable_workflow_v2_contract() -> dict[str, Any]:
    return {
        "summary": {
            "suite_name": "durable_workflow_engine_v2",
            "operator_status": "durable_workflow_engine_v2_no_persisted_receipts_visible",
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


def _redacted_durable_workflow_v2_contract(
    workflow_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    if not workflow_runs:
        return _empty_redacted_durable_workflow_v2_contract()

    contract = build_durable_workflow_v2_contract(workflow_runs)
    redacted_receipts = []
    for receipt in _as_list(contract.get("receipts")):
        receipt_dict = _as_dict(receipt)
        lease = _as_dict(receipt_dict.get("lease"))
        recovery = _as_dict(receipt_dict.get("recovery"))
        artifact_adoption = _as_dict(receipt_dict.get("artifact_adoption"))
        parent_identity = receipt_dict.get("parent_run_identity")
        redacted_receipts.append({
            "run_handle": f"workflow-run:{_digest(receipt_dict.get('run_identity'))}",
            "run_identity_digest": _digest(receipt_dict.get("run_identity")),
            "root_run_identity_digest": _digest(receipt_dict.get("root_run_identity")),
            "parent_run_identity_digest": _digest(parent_identity) if parent_identity else None,
            "workflow_name_digest": _digest(receipt_dict.get("workflow_name")),
            "status": receipt_dict.get("status"),
            "revision": int(receipt_dict.get("revision") or 0),
            "lease": {
                "owner_digest": _digest(lease.get("owner")) if lease.get("owner") else None,
                "lease_id_digest": _digest(lease.get("lease_id")) if lease.get("lease_id") else None,
                "expires_at": lease.get("expires_at"),
                "operator_visible": bool(lease.get("operator_visible")),
            },
            "lease_conflict_count": int(receipt_dict.get("lease_conflict_count") or 0),
            "latest_lease_conflict_digest": (
                _digest(receipt_dict.get("latest_lease_conflict"))
                if receipt_dict.get("latest_lease_conflict")
                else None
            ),
            "transition_count": int(receipt_dict.get("transition_count") or 0),
            "transition_keys_digest": _digest(receipt_dict.get("transition_keys")),
            "transition_block_count": int(receipt_dict.get("transition_block_count") or 0),
            "latest_transition_block_digest": (
                _digest(receipt_dict.get("latest_transition_block"))
                if receipt_dict.get("latest_transition_block")
                else None
            ),
            "trigger_count": int(receipt_dict.get("trigger_count") or 0),
            "deduped_trigger_count": int(receipt_dict.get("deduped_trigger_count") or 0),
            "recovery": {
                "status": recovery.get("status"),
                "reason": recovery.get("reason"),
                "receipt_digest": _digest(recovery) if recovery else None,
            },
            "artifact_adoption": {
                "kind": artifact_adoption.get("kind"),
                "status": artifact_adoption.get("status"),
                "blocked_reason": artifact_adoption.get("blocked_reason"),
                "receipt_digest": _digest(artifact_adoption) if artifact_adoption else None,
            },
            "approval_context_digest": receipt_dict.get("approval_context_digest"),
            "blocked_claims": list(_as_list(receipt_dict.get("blocked_claims"))),
            "claim_boundary": receipt_dict.get("claim_boundary"),
            "raw_payloads_redacted": True,
        })

    return {
        "summary": dict(_as_dict(contract.get("summary"))),
        "receipts": redacted_receipts,
        "policy": contract.get("policy", durable_workflow_v2_policy_payload()),
    }


def _empty_post_dp_contract() -> dict[str, Any]:
    return {
        "summary": {
            "suite_name": POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME,
            "operator_status": "post_dp_durable_orchestration_no_persisted_receipts_visible",
            "packet_count": 0,
            "ready_recovery_count": 0,
            "blocked_recovery_count": 0,
            "metadata_preservation_count": 0,
            "handoff_block_count": 0,
            "transition_block_count": 0,
            "deduped_trigger_count": 0,
            "trigger_external_action_allowed_count": 0,
            "side_effect_reconciliation_count": 0,
            "duplicate_suppression_count": 0,
            "guardian_restraint_count": 0,
            "unsafe_recovery_refusal_count": 0,
            "replay_window_count": 0,
            "all_raw_payloads_redacted": True,
            "claim_boundary": POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
        },
        "scenario_names": {
            POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME: list(POST_DP_DURABLE_ORCHESTRATION_SCENARIO_NAMES),
            MULTI_AGENT_HANDOFF_RECOVERY_SUITE_NAME: list(MULTI_AGENT_HANDOFF_RECOVERY_SCENARIO_NAMES),
            SCHEDULER_CRASH_RESTART_RECOVERY_SUITE_NAME: list(SCHEDULER_CRASH_RESTART_RECOVERY_SCENARIO_NAMES),
            SIDE_EFFECT_RECONCILIATION_V5_SUITE_NAME: list(SIDE_EFFECT_RECONCILIATION_V5_SCENARIO_NAMES),
            ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SUITE_NAME: list(ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES),
        },
        "recovery_packets": [],
        "durable_workflow_v2_contract": _empty_redacted_durable_workflow_v2_contract(),
        "policy": post_dp_durable_orchestration_policy_payload(),
    }


def build_post_dp_durable_orchestration_contract(
    workflow_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runs = _fixture_runs() if workflow_runs is None else workflow_runs
    packets = [_redacted_recovery_packet(run) for run in runs]
    v2_contract = _redacted_durable_workflow_v2_contract(runs)
    return {
        "summary": {
            "suite_name": POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME,
            "operator_status": "post_dp_durable_orchestration_gap_closure_visible",
            "packet_count": len(packets),
            "ready_recovery_count": sum(1 for packet in packets if packet["recovery_status"] == "ready"),
            "blocked_recovery_count": sum(1 for packet in packets if packet["recovery_status"] == "blocked"),
            "metadata_preservation_count": sum(
                1 for packet in packets if packet["restart_preserved_metadata"]
            ),
            "handoff_block_count": sum(int(packet["handoff_block_count"]) for packet in packets),
            "transition_block_count": sum(int(packet["transition_block_count"]) for packet in packets),
            "deduped_trigger_count": sum(int(packet["deduped_trigger_count"]) for packet in packets),
            "trigger_external_action_allowed_count": sum(
                int(packet["trigger_external_action_allowed_count"]) for packet in packets
            ),
            "side_effect_reconciliation_count": sum(
                1 for packet in packets if packet["side_effect_boundary"]["reconciliation_status"]
            ),
            "duplicate_suppression_count": sum(
                1 for packet in packets if packet["side_effect_boundary"]["duplicate_suppressed"]
            ),
            "guardian_restraint_count": sum(
                1 for packet in packets
                if packet["guardian_recovery"]["restraint_posture"]
                and not packet["guardian_recovery"]["authority_expanded"]
            ),
            "unsafe_recovery_refusal_count": sum(
                int(packet["unsafe_recovery_refusal_count"]) for packet in packets
            ),
            "replay_window_count": sum(
                1 for packet in packets if packet["replay_window"]["authority_required"]
            ),
            "all_raw_payloads_redacted": all(packet["raw_payloads_redacted"] for packet in packets),
            "claim_boundary": POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
        },
        "scenario_names": {
            POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME: list(POST_DP_DURABLE_ORCHESTRATION_SCENARIO_NAMES),
            MULTI_AGENT_HANDOFF_RECOVERY_SUITE_NAME: list(MULTI_AGENT_HANDOFF_RECOVERY_SCENARIO_NAMES),
            SCHEDULER_CRASH_RESTART_RECOVERY_SUITE_NAME: list(SCHEDULER_CRASH_RESTART_RECOVERY_SCENARIO_NAMES),
            SIDE_EFFECT_RECONCILIATION_V5_SUITE_NAME: list(SIDE_EFFECT_RECONCILIATION_V5_SCENARIO_NAMES),
            ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SUITE_NAME: list(ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES),
        },
        "recovery_packets": packets,
        "durable_workflow_v2_contract": v2_contract,
        "policy": post_dp_durable_orchestration_policy_payload(),
    }


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failed = int(getattr(summary, "failed", 0) or 0)
    if failed == 0:
        return []
    return [
        {
            "suite": POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME,
            "failed": str(failed),
            "summary": "Post-DP durable orchestration benchmark reported regressions.",
        }
    ]


async def _run_post_dp_durable_orchestration_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME,
        MULTI_AGENT_HANDOFF_RECOVERY_SUITE_NAME,
        SCHEDULER_CRASH_RESTART_RECOVERY_SUITE_NAME,
        SIDE_EFFECT_RECONCILIATION_V5_SUITE_NAME,
        ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    ])


async def build_post_dp_durable_orchestration_report() -> dict[str, Any]:
    summary = await _run_post_dp_durable_orchestration_suites()
    failures = _failure_report(summary)
    try:
        persisted_runs = await workflow_state_repository.list_runs(limit=25)
    except Exception:  # pragma: no cover - defensive local DB compatibility.
        persisted_runs = []
    dq_persisted_runs = [run for run in persisted_runs if _has_dq_persisted_evidence(run)]
    proof_contract = build_post_dp_durable_orchestration_contract()
    persisted_contract = (
        build_post_dp_durable_orchestration_contract(dq_persisted_runs)
        if dq_persisted_runs
        else _empty_post_dp_contract()
    )
    active_contract = persisted_contract if dq_persisted_runs else proof_contract
    evidence_mode = "persisted_runtime_receipts" if dq_persisted_runs else "deterministic_fixture_receipts"
    return {
        "summary": {
            "suite_name": POST_DP_DURABLE_ORCHESTRATION_SUITE_NAME,
            "benchmark_posture": (
                "post_dp_durable_orchestration_ci_gated_operator_visible"
                if not failures
                else "post_dp_durable_orchestration_regressions_detected"
            ),
            "operator_status": "post_dp_durable_orchestration_gap_closure_visible",
            "suite_count": 5,
            "scenario_count": (
                len(POST_DP_DURABLE_ORCHESTRATION_SCENARIO_NAMES)
                + len(MULTI_AGENT_HANDOFF_RECOVERY_SCENARIO_NAMES)
                + len(SCHEDULER_CRASH_RESTART_RECOVERY_SCENARIO_NAMES)
                + len(SIDE_EFFECT_RECONCILIATION_V5_SCENARIO_NAMES)
                + len(ORCHESTRATION_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
            ),
            "persisted_run_count": len(persisted_runs),
            "persisted_dq_run_count": len(dq_persisted_runs),
            "evidence_mode": evidence_mode,
            "packet_count": active_contract["summary"]["packet_count"],
            "ready_recovery_count": active_contract["summary"]["ready_recovery_count"],
            "blocked_recovery_count": active_contract["summary"]["blocked_recovery_count"],
            "metadata_preservation_count": active_contract["summary"]["metadata_preservation_count"],
            "handoff_block_count": active_contract["summary"]["handoff_block_count"],
            "transition_block_count": active_contract["summary"]["transition_block_count"],
            "deduped_trigger_count": active_contract["summary"]["deduped_trigger_count"],
            "trigger_external_action_allowed_count": active_contract["summary"][
                "trigger_external_action_allowed_count"
            ],
            "side_effect_reconciliation_count": active_contract["summary"]["side_effect_reconciliation_count"],
            "duplicate_suppression_count": active_contract["summary"]["duplicate_suppression_count"],
            "guardian_restraint_count": active_contract["summary"]["guardian_restraint_count"],
            "unsafe_recovery_refusal_count": active_contract["summary"]["unsafe_recovery_refusal_count"],
            "replay_window_count": active_contract["summary"]["replay_window_count"],
            "all_raw_payloads_redacted": active_contract["summary"]["all_raw_payloads_redacted"],
            "claim_boundary": POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
        },
        "failure_report": failures,
        "policy": post_dp_durable_orchestration_policy_payload(),
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
