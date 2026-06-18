from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select

from src.audit.repository import audit_repository
from src.db.engine import get_session
from src.db.models import AuditEvent, Memory, MemorySource, MemoryStatus
from src.memory.benchmark import build_guardian_memory_benchmark_report
from src.memory.decay import memory_reconciliation_policy_payload, summarize_memory_reconciliation_state
from src.memory.repository import memory_repository

M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME = "m6_memory_superiority"
M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES = (
    "m6_behavior_changing_recall_receipt_behavior",
    "m6_operator_memory_control_receipts_behavior",
    "m6_privacy_and_provenance_boundary_behavior",
    "m6_stale_contradiction_suppression_behavior",
    "m6_operator_surface_behavior",
)

_CONTROL_EVENT_TYPES = {
    "memory_corrected",
    "memory_pinned",
    "memory_forgotten",
    "memory_audited",
}
_CONTROL_EVENT_ACTIONS = {
    "memory_corrected": "correct",
    "memory_pinned": "pin",
    "memory_forgotten": "forget",
    "memory_audited": "audit",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _metadata(memory: Memory) -> dict[str, Any]:
    try:
        payload = json.loads(memory.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _memory_text(memory: Memory) -> str:
    return (memory.summary or memory.content or "").strip()


def m6_memory_superiority_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "behavior_changing_recall",
            "label": "Behavior-changing recall",
            "summary": "Memory should change action posture, intervention timing, suppression, or capability choice with an inspectable receipt.",
        },
        {
            "name": "operator_control",
            "label": "Operator control",
            "summary": "Operators can correct, pin, forget, and audit canonical memories without source diving.",
        },
        {
            "name": "provenance_and_trust",
            "label": "Provenance and trust",
            "summary": "Memory records expose source count, source types, confidence, recency, and conflict state.",
        },
        {
            "name": "privacy_boundary",
            "label": "Privacy boundary",
            "summary": "Private or scoped memories stay explicitly labeled so recall and provider writeback do not widen authority.",
        },
        {
            "name": "stale_conflict_suppression",
            "label": "Stale and conflict suppression",
            "summary": "Archived, superseded, and stale contradictory memories remain visible as receipts but not as current truth.",
        },
        {
            "name": "provider_quality_gate",
            "label": "Provider quality gate",
            "summary": "External memory providers are advisory and quality-gated behind canonical guardian memory.",
        },
    ]


def m6_memory_superiority_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "inert_recall",
            "severity": "high",
            "summary": "Relevant memory is present but no decision, timing, restraint, or capability selection changes.",
        },
        {
            "name": "unaudited_operator_mutation",
            "severity": "high",
            "summary": "A memory correction, pin, forget, or audit action lacks a durable audit receipt.",
        },
        {
            "name": "privacy_boundary_drift",
            "severity": "high",
            "summary": "A private/scoped memory can be treated as provider-writeback or broad recall material without boundary metadata.",
        },
        {
            "name": "stale_conflict_leak",
            "severity": "high",
            "summary": "Archived, superseded, or stale contradictory memory re-enters current decision context.",
        },
        {
            "name": "missing_provenance",
            "severity": "medium",
            "summary": "Operator memory records lack source type, session, confidence, or recency evidence.",
        },
        {
            "name": "provider_quality_drift",
            "severity": "medium",
            "summary": "External provider evidence becomes authoritative instead of advisory to canonical guardian memory.",
        },
    ]


def m6_memory_superiority_policy_payload() -> dict[str, Any]:
    reconciliation = memory_reconciliation_policy_payload()
    return {
        **reconciliation,
        "benchmark_suite": M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME,
        "behavior_change_policy": "memory_must_emit_decision_or_restraint_receipts",
        "operator_control_policy": "correct_pin_forget_audit_with_durable_receipts",
        "privacy_boundary_policy": "canonical_memory_private_scope_blocks_provider_authority_widening",
        "provider_quality_policy": "advisory_provider_evidence_requires_canonical_anchor",
        "receipt_surfaces": [
            "operator_m6_memory_superiority",
            "audit_events",
            "guardian_state_judgment_proof",
            "memory_benchmark",
        ],
        "control_actions": ["correct", "pin", "forget", "audit"],
        "ci_gate_mode": "required_benchmark_suite",
    }


async def _memory_counts() -> dict[str, int]:
    async with get_session() as db:
        active = int((await db.execute(select(func.count()).select_from(Memory).where(Memory.status == MemoryStatus.active))).scalar_one() or 0)
        superseded = int((await db.execute(select(func.count()).select_from(Memory).where(Memory.status == MemoryStatus.superseded))).scalar_one() or 0)
        archived = int((await db.execute(select(func.count()).select_from(Memory).where(Memory.status == MemoryStatus.archived))).scalar_one() or 0)
        sources = int((await db.execute(select(func.count()).select_from(MemorySource))).scalar_one() or 0)
    return {
        "active": active,
        "superseded": superseded,
        "archived": archived,
        "source": sources,
    }


async def _list_memory_records(*, limit: int = 8) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    memories: list[Memory] = []
    for status in (MemoryStatus.active, MemoryStatus.superseded, MemoryStatus.archived):
        memories.extend(await memory_repository.list_memories(status=status, limit=limit))

    ordered = sorted(
        memories,
        key=lambda item: (item.updated_at or item.created_at or _now()),
        reverse=True,
    )[:limit]
    for memory in ordered:
        metadata = _metadata(memory)
        sources = await memory_repository.list_sources(memory_id=memory.id)
        source_types = sorted({source.source_type for source in sources if source.source_type})
        privacy_boundary = str(
            metadata.get("privacy_boundary")
            or metadata.get("privacy_scope")
            or metadata.get("memory_scope")
            or "standard"
        )
        records.append(
            {
                "id": memory.id,
                "kind": memory.kind.value,
                "status": memory.status.value,
                "summary": _memory_text(memory),
                "content": memory.content,
                "confidence": memory.confidence,
                "importance": memory.importance,
                "reinforcement": memory.reinforcement,
                "last_confirmed_at": memory.last_confirmed_at.isoformat() if memory.last_confirmed_at else None,
                "updated_at": memory.updated_at.isoformat(),
                "provenance": {
                    "source_session_id": memory.source_session_id,
                    "source_count": len(sources),
                    "source_types": source_types,
                    "has_source_receipt": bool(sources or memory.source_session_id),
                },
                "control": {
                    "pinned": bool(metadata.get("operator_pinned")),
                    "corrected": bool(metadata.get("operator_corrected_at")),
                    "forgotten": memory.status == MemoryStatus.archived and metadata.get("archived_reason") == "operator_forget",
                    "privacy_boundary": privacy_boundary,
                    "provider_writeback_allowed": privacy_boundary in {"standard", "shared"} and not bool(metadata.get("private")),
                },
                "conflict": {
                    "superseded_by_memory_id": metadata.get("superseded_by_memory_id"),
                    "superseded_reason": metadata.get("superseded_reason"),
                    "archived_reason": metadata.get("archived_reason"),
                },
            }
        )
    return records


async def _recent_control_receipts(*, limit: int = 8) -> list[dict[str, Any]]:
    async with get_session() as db:
        events = (
            await db.execute(
                select(AuditEvent)
                .where(col(AuditEvent.event_type).in_(_CONTROL_EVENT_TYPES))
                .order_by(col(AuditEvent.created_at).desc())
                .limit(limit)
            )
        ).scalars().all()
        for event in events:
            db.expunge(event)
    receipts: list[dict[str, Any]] = []
    for event in events:
        try:
            details = json.loads(event.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        if not isinstance(details, dict):
            details = {}
        receipts.append(
            {
                "id": event.id,
                "action": _CONTROL_EVENT_ACTIONS.get(event.event_type, event.event_type),
                "event_type": event.event_type,
                "memory_id": details.get("memory_id"),
                "summary": event.summary,
                "risk_level": event.risk_level,
                "session_id": event.session_id,
                "created_at": event.created_at.isoformat(),
                "details": details,
            }
        )
    return receipts


def behavior_change_receipt_from_guardian_state(state: Any) -> dict[str, Any]:
    changed_dimensions: list[str] = []
    evidence: list[str] = []
    memory_context = str(getattr(state, "memory_context", "") or "")
    learning_guidance = str(getattr(state, "learning_guidance", "") or "")
    action_posture = str(getattr(state, "action_posture", "act_when_grounded") or "act_when_grounded")
    intent_resolution = str(getattr(state, "intent_resolution", "proceed") or "proceed")
    restraint_reasons = [str(item) for item in getattr(state, "restraint_reasons", ()) or () if str(item).strip()]
    judgment_proof = [str(item) for item in getattr(state, "judgment_proof_lines", ()) or () if str(item).strip()]
    diagnostics = [str(item) for item in getattr(state, "memory_reconciliation_diagnostics", ()) or () if str(item).strip()]
    decision_receipt = getattr(state, "memory_decision_receipt", {}) or {}
    if isinstance(decision_receipt, dict) and decision_receipt.get("changed_decision"):
        changed_dimensions.append("memory_decision_receipt")
        evidence.append(
            "memory_decision_receipt="
            + str(decision_receipt.get("intervention_timing") or decision_receipt.get("lane") or "present")
        )

    if memory_context.strip():
        changed_dimensions.append("recall_context")
        evidence.append("relevant_memory_context_present")
    if learning_guidance.strip():
        changed_dimensions.append("intervention_policy")
        evidence.append("procedural_memory_guidance_present")
    if action_posture != "act_when_grounded":
        changed_dimensions.append("action_posture")
        evidence.append(f"action_posture={action_posture}")
    if intent_resolution != "proceed":
        changed_dimensions.append("intent_resolution")
        evidence.append(f"intent_resolution={intent_resolution}")
    if restraint_reasons:
        changed_dimensions.append("suppression_or_timing")
        evidence.extend(restraint_reasons[:2])
    if judgment_proof:
        changed_dimensions.append("judgment_proof")
        evidence.extend(judgment_proof[:2])

    return {
        "id": "guardian-state-memory-influence",
        "changed": bool(changed_dimensions),
        "changed_dimensions": list(dict.fromkeys(changed_dimensions)),
        "action_posture": action_posture,
        "intent_resolution": intent_resolution,
        "memory_confidence": getattr(getattr(state, "confidence", None), "memory", "unknown"),
        "evidence": evidence[:8],
        "diagnostics": diagnostics[:6],
        "decision_receipt": decision_receipt if isinstance(decision_receipt, dict) else {},
        "receipt_contract": "memory_changed_or_explained_guardian_behavior",
    }


async def apply_memory_operator_control(
    *,
    memory_id: str,
    action: str,
    note: str | None = None,
    content: str | None = None,
    summary: str | None = None,
    privacy_boundary: str | None = None,
    session_id: str | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"correct", "pin", "forget", "audit"}:
        raise ValueError("action must be one of: correct, pin, forget, audit")
    memory = await memory_repository.get_memory(memory_id)
    if memory is None:
        raise ValueError(f"Unknown memory id: {memory_id}")

    timestamp = _now()
    metadata: dict[str, Any] = {
        "operator_control_last_action": normalized_action,
        "operator_control_note": str(note or "").strip(),
        "operator_control_at": timestamp.isoformat(),
    }
    if privacy_boundary:
        metadata["privacy_boundary"] = str(privacy_boundary).strip()

    update_kwargs: dict[str, Any] = {"metadata": metadata}
    risk_level = "low"
    if normalized_action == "correct":
        metadata["operator_corrected_at"] = timestamp.isoformat()
        update_kwargs.update(
            {
                "content": content if content is not None else memory.content,
                "summary": summary if summary is not None else memory.summary,
                "confidence": max(memory.confidence, 0.85),
                "importance": max(memory.importance, 0.75),
                "status": MemoryStatus.active,
                "last_confirmed_at": timestamp,
            }
        )
        event_type = "memory.corrected"
        risk_level = "medium"
    elif normalized_action == "pin":
        metadata["operator_pinned"] = True
        metadata["operator_pinned_at"] = timestamp.isoformat()
        update_kwargs.update(
            {
                "confidence": max(memory.confidence, 0.8),
                "importance": max(memory.importance, 0.95),
                "reinforcement": max(memory.reinforcement, 2.0),
                "status": MemoryStatus.active,
                "last_confirmed_at": timestamp,
            }
        )
        event_type = "memory.pinned"
    elif normalized_action == "forget":
        metadata["operator_forgotten_at"] = timestamp.isoformat()
        metadata["archived_reason"] = "operator_forget"
        update_kwargs.update({"status": MemoryStatus.archived})
        event_type = "memory.forgotten"
        risk_level = "medium"
    else:
        metadata["operator_audited_at"] = timestamp.isoformat()
        event_type = "memory.audited"

    updated = await memory_repository.update_memory_control(memory_id, **update_kwargs)
    audit = await audit_repository.log_event(
        event_type=event_type,
        actor=actor,
        session_id=session_id,
        tool_name="memory_operator_control",
        risk_level=risk_level,
        summary=f"Memory {normalized_action}: {_memory_text(updated)}",
        details={
            "memory_id": memory_id,
            "action": normalized_action,
            "note": str(note or "").strip(),
            "privacy_boundary": privacy_boundary,
            "status": updated.status.value,
            "kind": updated.kind.value,
        },
    )
    return {
        "memory_id": updated.id,
        "action": normalized_action,
        "status": updated.status.value,
        "audit_event_id": audit.id,
        "receipt_state": "durable_audit_recorded",
    }


async def build_m6_memory_superiority_payload(
    *,
    session_id: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    from src.guardian.state import build_guardian_state

    state = await build_guardian_state(
        session_id=session_id,
        user_message=query or "What should memory change before Seraph acts?",
    )
    counts = await _memory_counts()
    reconciliation = await summarize_memory_reconciliation_state()
    memory_benchmark = await build_guardian_memory_benchmark_report(
        run_suite=False,
        reconciliation=reconciliation,
    )
    records = await _list_memory_records(limit=10)
    receipts = await _recent_control_receipts(limit=8)
    behavior_receipt = behavior_change_receipt_from_guardian_state(state)
    privacy_boundaries = sorted(
        {
            str(record.get("control", {}).get("privacy_boundary") or "standard")
            for record in records
            if isinstance(record.get("control"), dict)
        }
    )
    provider_blocked_count = sum(
        1
        for record in records
        if isinstance(record.get("control"), dict)
        and not bool(record["control"].get("provider_writeback_allowed"))
    )
    return {
        "summary": {
            "operator_status": "m6_memory_superiority_visible",
            "active_memory_count": counts["active"],
            "superseded_memory_count": counts["superseded"],
            "archived_memory_count": counts["archived"],
            "source_receipt_count": counts["source"],
            "control_receipt_count": len(receipts),
            "behavior_receipt_count": 1 if behavior_receipt["changed"] else 0,
            "privacy_boundary_count": len(privacy_boundaries),
            "provider_writeback_blocked_count": provider_blocked_count,
            "memory_confidence": getattr(getattr(state, "confidence", None), "memory", "unknown"),
            "action_posture": behavior_receipt["action_posture"],
            "claim_boundary": "deterministic_operator_memory_control_and_behavior_receipts_not_live_external_memory_parity",
        },
        "behavior_receipts": [behavior_receipt],
        "memory_records": records,
        "control_receipts": receipts,
        "privacy_boundaries": privacy_boundaries,
        "reconciliation": reconciliation,
        "benchmark": memory_benchmark,
        "policy": m6_memory_superiority_policy_payload(),
    }


def _suite_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "m6_benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "M6 memory superiority scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:6]


def _placeholder_summary():
    from types import SimpleNamespace

    return SimpleNamespace(total=None, passed=None, failed=0, duration_ms=None, results=[])


async def _run_m6_memory_superiority_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME])


async def build_m6_memory_superiority_benchmark_report(*, run_suite: bool = True) -> dict[str, Any]:
    summary = await _run_m6_memory_superiority_benchmark_suite() if run_suite else _placeholder_summary()
    failures = _suite_failure_report(summary) if run_suite else []
    return {
        "summary": {
            "suite_name": M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME,
            "benchmark_posture": (
                "ci_gated_operator_visible"
                if int(getattr(summary, "failed", 0) or 0) == 0 and run_suite
                else "suite_contract_visible_not_run"
                if not run_suite
                else "ci_regressions_detected_operator_visible"
            ),
            "operator_status": "m6_memory_superiority_visible",
            "scenario_count": len(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(m6_memory_superiority_dimensions()),
            "failure_mode_count": len(m6_memory_superiority_failure_taxonomy()),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
            "behavior_change_state": "receipt_required",
            "operator_control_state": "correct_pin_forget_audit",
            "privacy_boundary_state": "provider_authority_guarded",
        },
        "scenario_names": list(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES),
        "dimensions": m6_memory_superiority_dimensions(),
        "failure_taxonomy": m6_memory_superiority_failure_taxonomy(),
        "failure_report": failures,
        "policy": m6_memory_superiority_policy_payload(),
        "latest_run": {
            "total": getattr(summary, "total", None),
            "passed": getattr(summary, "passed", None),
            "failed": getattr(summary, "failed", 0),
            "duration_ms": getattr(summary, "duration_ms", None),
            "executed": run_suite,
        },
    }


# Keep historical imports from this module aligned with the canonical M6
# benchmark contract used by the catalog, eval harness, and operator API.
from src.memory.superiority_benchmark import (  # noqa: E402
    M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES,
    M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME,
    build_m6_memory_superiority_benchmark_report,
)
