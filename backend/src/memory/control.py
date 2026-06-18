from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.audit.repository import audit_repository
from src.db.models import Memory, MemoryEdgeType, MemoryKind, MemoryStatus
from src.memory.decay import apply_memory_decay_policies, summarize_memory_reconciliation_state
from src.memory.providers import list_memory_provider_inventory
from src.memory.repository import memory_repository
from src.memory.types import kind_to_category, normalize_memory_kind


_PRIVACY_BOUNDARIES = {
    "operator_visible",
    "private",
    "sensitive",
    "source_bound",
}
_TRUSTED_METADATA_KEYS = {"privacy_boundary", "provenance", "operator_control"}
_LIVE_CONTROL_ACTIONS = {
    "review_outcome",
    "decay_stale_evidence",
    "rollback_memory",
    "propagate_delete_export",
    "quarantine_provider",
    "reinstate_provider",
}
_REVIEW_OUTCOME_ALIASES = {
    "accepted": "accepted",
    "helpful": "accepted",
    "rejected": "rejected",
    "harmful": "rejected",
    "not_helpful": "rejected",
    "needs_follow_up": "needs_follow_up",
    "ignored": "needs_follow_up",
    "corrected": "needs_follow_up",
}
_BLOCKED_LIVE_CONTROL_CLAIMS = [
    "solved_guardian_learning",
    "solved_learning",
    "solved_memory",
    "guardian_or_memory_superiority",
    "guardian_intelligence_superiority",
    "live_human_outcome_superiority",
    "generalized_outcome_superiority",
    "memory_superiority",
    "best_in_class_memory",
    "full_memory_provider_parity",
    "complete_provider_delete_export_propagation",
    "production_readiness",
    "full_parity",
    "reference_system_exceedance",
]
_PROVIDER_QUARANTINES: dict[str, dict[str, Any]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_score(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return default


def _normalize_privacy_boundary(value: str | None) -> str:
    normalized = str(value or "operator_visible").strip().lower()
    if normalized in _PRIVACY_BOUNDARIES:
        return normalized
    raise ValueError(f"unknown privacy_boundary: {normalized}")


def _safe_privacy_boundary(value: Any) -> str:
    try:
        return _normalize_privacy_boundary(str(value) if value is not None else None)
    except ValueError:
        return "unknown"


def _caller_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key) not in _TRUSTED_METADATA_KEYS
    }


def _metadata(memory: Memory) -> dict[str, Any]:
    try:
        payload = json.loads(memory.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _memory_payload(memory: Memory) -> dict[str, Any]:
    metadata = _metadata(memory)
    return {
        "id": memory.id,
        "content": memory.content,
        "summary": memory.summary,
        "kind": memory.kind.value,
        "category": memory.category.value,
        "status": memory.status.value,
        "confidence": memory.confidence,
        "importance": memory.importance,
        "reinforcement": memory.reinforcement,
        "source_session_id": memory.source_session_id,
        "subject_entity_id": memory.subject_entity_id,
        "project_entity_id": memory.project_entity_id,
        "last_confirmed_at": memory.last_confirmed_at.isoformat() if memory.last_confirmed_at else None,
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
        "metadata": metadata,
        "provenance": metadata.get("provenance") or {},
        "privacy_boundary": _safe_privacy_boundary(metadata.get("privacy_boundary")),
        "operator_control": metadata.get("operator_control") or {},
    }


@dataclass(frozen=True)
class MemoryControlReceipt:
    action: str
    memory_id: str
    actor: str
    changed_memory: bool
    changed_decision: bool
    provenance: str
    confidence: float
    recency: str
    conflict_policy: str
    privacy_boundary: str
    intervention_timing: str
    suppression_state: str
    capability_choice: str
    audit_event_type: str
    corrected_memory_id: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def memory_operator_policy_payload() -> dict[str, Any]:
    return {
        "authoritative_memory": "guardian",
        "operator_authority": "operator_corrections_override_agent_extraction",
        "control_primitives": ["correct", "pin", "forget", "audit", *sorted(_LIVE_CONTROL_ACTIONS)],
        "provenance_values": [
            "operator_correction",
            "operator_pin",
            "operator_forget",
            "guardian_canonical",
            "external_advisory",
        ],
        "privacy_boundaries": sorted(_PRIVACY_BOUNDARIES),
        "conflict_policy": "operator_correction_supersedes_conflicting_memory",
        "recency_policy": "operator_actions_refresh_last_confirmed_at",
        "receipt_policy": "every_operator_memory_action_emits_auditable_receipt",
        "acknowledgement_policy": "live_control_actions_require_explicit_operator_acknowledgement",
        "claim_boundary": "live_controls_are_operator_receipts_not_solved_learning_superiority_or_full_provider_parity",
        "blocked_claims": list(_BLOCKED_LIVE_CONTROL_CLAIMS),
    }


def _operator_metadata(
    *,
    action: str,
    actor: str,
    privacy_boundary: str,
    reason: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    occurred_at = _now().isoformat()
    return {
        "privacy_boundary": privacy_boundary,
        "provenance": {
            "kind": f"operator_{action}",
            "actor": actor,
            "source": "operator_api",
            "privacy_boundary": privacy_boundary,
            "recorded_at": occurred_at,
        },
        "operator_control": {
            "last_action": action,
            "last_actor": actor,
            "last_reason": str(reason or "").strip(),
            "last_action_at": occurred_at,
            **(extra or {}),
        },
    }


async def correct_memory(
    *,
    content: str,
    kind: MemoryKind | str = MemoryKind.fact,
    summary: str | None = None,
    corrects_memory_id: str | None = None,
    source_session_id: str | None = None,
    actor: str = "operator",
    reason: str | None = None,
    confidence: float = 0.95,
    importance: float = 0.9,
    privacy_boundary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_content = " ".join(str(content or "").strip().split())
    if not normalized_content:
        raise ValueError("content must be non-empty")
    normalized_kind = normalize_memory_kind(kind)
    boundary = _normalize_privacy_boundary(privacy_boundary)
    now = _now()
    metadata_updates = {
        **_caller_metadata(metadata),
        **_operator_metadata(
            action="correction",
            actor=actor,
            privacy_boundary=boundary,
            reason=reason,
            extra={"pinned": False},
        ),
    }

    created = await memory_repository.create_memory(
        content=normalized_content,
        kind=normalized_kind,
        category=kind_to_category(normalized_kind),
        source_session_id=source_session_id,
        source_type="operator",
        source_snippet=reason,
        summary=(summary.strip() if isinstance(summary, str) and summary.strip() else None),
        confidence=_clamp_score(confidence, default=0.95),
        importance=_clamp_score(importance, default=0.9),
        reinforcement=1.5,
        metadata=metadata_updates,
        last_confirmed_at=now,
    )
    memory = await memory_repository.get_memory(created.memory_id)
    if memory is None:  # pragma: no cover - defensive, create_memory already flushed
        raise ValueError(f"Unknown memory id: {created.memory_id}")

    corrected_memory = None
    if corrects_memory_id:
        corrected_memory = await memory_repository.update_memory_control_metadata(
            corrects_memory_id,
            status=MemoryStatus.superseded,
            metadata_updates={
                "superseded_reason": "operator_correction",
                "superseded_by_memory_id": memory.id,
                "operator_control": {
                    "last_action": "superseded_by_operator_correction",
                    "last_actor": actor,
                    "last_reason": str(reason or "").strip(),
                    "last_action_at": now.isoformat(),
                },
            },
        )
        await memory_repository.create_edge(
            from_memory_id=memory.id,
            to_memory_id=corrected_memory.id,
            edge_type=MemoryEdgeType.supersedes,
            metadata={"reason": "operator_correction", "actor": actor},
        )
        await memory_repository.create_edge(
            from_memory_id=memory.id,
            to_memory_id=corrected_memory.id,
            edge_type=MemoryEdgeType.contradicts,
            metadata={"reason": "operator_correction", "actor": actor},
        )

    audit_event = await audit_repository.log_event(
        actor=actor,
        event_type="memory_corrected",
        tool_name="memory_control",
        risk_level="medium" if boundary in {"sensitive", "source_bound"} else "low",
        policy_mode="operator_controlled",
        session_id=source_session_id,
        summary="Operator corrected guardian memory",
        details={
            "memory_id": memory.id,
            "corrected_memory_id": corrects_memory_id,
            "kind": normalized_kind.value,
            "privacy_boundary": boundary,
            "reason": reason,
        },
    )
    receipt = MemoryControlReceipt(
        action="correct",
        memory_id=memory.id,
        corrected_memory_id=corrects_memory_id,
        actor=actor,
        changed_memory=True,
        changed_decision=True,
        provenance="operator_correction",
        confidence=memory.confidence,
        recency="refreshed_now",
        conflict_policy="operator_correction_supersedes_conflicting_memory",
        privacy_boundary=boundary,
        intervention_timing="next_retrieval",
        suppression_state="corrected_memory_superseded" if corrected_memory else "none",
        capability_choice="guardian_canonical_memory",
        audit_event_type="memory_corrected",
    )
    return {
        "memory": _memory_payload(memory),
        "corrected_memory": _memory_payload(corrected_memory) if corrected_memory else None,
        "receipt": receipt.as_payload(),
        "audit_event_id": audit_event.id,
        "policy": memory_operator_policy_payload(),
    }


async def pin_memory(
    *,
    memory_id: str,
    actor: str = "operator",
    reason: str | None = None,
    privacy_boundary: str | None = None,
) -> dict[str, Any]:
    boundary = _normalize_privacy_boundary(privacy_boundary)
    memory = await memory_repository.update_memory_control_metadata(
        memory_id,
        status=MemoryStatus.active,
        confidence=1.0,
        importance=1.0,
        reinforcement=2.0,
        last_confirmed_at=_now(),
        metadata_updates=_operator_metadata(
            action="pin",
            actor=actor,
            privacy_boundary=boundary,
            reason=reason,
            extra={"pinned": True},
        ),
    )
    audit_event = await audit_repository.log_event(
        actor=actor,
        event_type="memory_pinned",
        tool_name="memory_control",
        risk_level="low",
        policy_mode="operator_controlled",
        session_id=memory.source_session_id,
        summary="Operator pinned guardian memory",
        details={
            "memory_id": memory.id,
            "kind": memory.kind.value,
            "privacy_boundary": boundary,
            "reason": reason,
        },
    )
    receipt = MemoryControlReceipt(
        action="pin",
        memory_id=memory.id,
        actor=actor,
        changed_memory=True,
        changed_decision=True,
        provenance="operator_pin",
        confidence=memory.confidence,
        recency="refreshed_now",
        conflict_policy="pinned_memory_ranks_as_operator_confirmed",
        privacy_boundary=boundary,
        intervention_timing="next_retrieval",
        suppression_state="none",
        capability_choice="guardian_canonical_memory",
        audit_event_type="memory_pinned",
    )
    return {
        "memory": _memory_payload(memory),
        "receipt": receipt.as_payload(),
        "audit_event_id": audit_event.id,
        "policy": memory_operator_policy_payload(),
    }


async def forget_memory(
    *,
    memory_id: str,
    actor: str = "operator",
    reason: str | None = None,
    mode: str = "archive",
    privacy_boundary: str | None = None,
) -> dict[str, Any]:
    normalized_mode = "redact" if str(mode or "").strip().lower() == "redact" else "archive"
    boundary = _normalize_privacy_boundary(privacy_boundary)
    update_kwargs: dict[str, Any] = {
        "status": MemoryStatus.archived,
        "confidence": 0.0,
        "importance": 0.0,
        "reinforcement": 0.0,
        "metadata_updates": {
            **_operator_metadata(
                action="forget",
                actor=actor,
                privacy_boundary=boundary,
                reason=reason,
                extra={"forget_mode": normalized_mode, "pinned": False},
            ),
            "archived_reason": "operator_forget",
        },
    }
    if normalized_mode == "redact":
        update_kwargs["content"] = "[forgotten by operator]"
        update_kwargs["summary"] = "[forgotten by operator]"
    memory = await memory_repository.update_memory_control_metadata(memory_id, **update_kwargs)
    audit_event = await audit_repository.log_event(
        actor=actor,
        event_type="memory_forgotten",
        tool_name="memory_control",
        risk_level="medium" if normalized_mode == "redact" else "low",
        policy_mode="operator_controlled",
        session_id=memory.source_session_id,
        summary="Operator forgot guardian memory",
        details={
            "memory_id": memory.id,
            "kind": memory.kind.value,
            "privacy_boundary": boundary,
            "mode": normalized_mode,
            "reason": reason,
        },
    )
    receipt = MemoryControlReceipt(
        action="forget",
        memory_id=memory.id,
        actor=actor,
        changed_memory=True,
        changed_decision=True,
        provenance="operator_forget",
        confidence=memory.confidence,
        recency="suppressed_now",
        conflict_policy="operator_forget_removes_memory_from_active_retrieval",
        privacy_boundary=boundary,
        intervention_timing="immediate_suppression",
        suppression_state="archived_status",
        capability_choice="guardian_canonical_memory",
        audit_event_type="memory_forgotten",
    )
    return {
        "memory": _memory_payload(memory),
        "receipt": receipt.as_payload(),
        "audit_event_id": audit_event.id,
        "policy": memory_operator_policy_payload(),
    }


async def audit_memory(
    *,
    memory_id: str,
    actor: str = "operator",
    reason: str | None = None,
) -> dict[str, Any]:
    memory = await memory_repository.get_memory(memory_id)
    if memory is None:
        raise ValueError(f"Unknown memory id: {memory_id}")
    metadata = _metadata(memory)
    boundary = _normalize_privacy_boundary(metadata.get("privacy_boundary"))
    audit_event = await audit_repository.log_event(
        actor=actor,
        event_type="memory_audited",
        tool_name="memory_control",
        risk_level="low",
        policy_mode="operator_controlled",
        session_id=memory.source_session_id,
        summary="Operator audited guardian memory",
        details={
            "memory_id": memory.id,
            "kind": memory.kind.value,
            "privacy_boundary": boundary,
            "reason": reason,
        },
    )
    receipt = MemoryControlReceipt(
        action="audit",
        memory_id=memory.id,
        actor=actor,
        changed_memory=False,
        changed_decision=False,
        provenance=str((metadata.get("provenance") or {}).get("kind") or "guardian_canonical"),
        confidence=memory.confidence,
        recency="inspected_now",
        conflict_policy="operator_audit_does_not_mutate_memory",
        privacy_boundary=boundary,
        intervention_timing="none",
        suppression_state=str(metadata.get("archived_reason") or metadata.get("superseded_reason") or "none"),
        capability_choice="guardian_canonical_memory",
        audit_event_type="memory_audited",
    )
    return {
        "memory": _memory_payload(memory),
        "receipt": receipt.as_payload(),
        "audit_event_id": audit_event.id,
        "policy": memory_operator_policy_payload(),
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
    if normalized_action == "correct":
        existing = await memory_repository.get_memory(memory_id)
        if existing is None:
            raise ValueError(f"Unknown memory id: {memory_id}")
        return await correct_memory(
            content=content or existing.content,
            kind=existing.kind,
            summary=summary if summary is not None else existing.summary,
            corrects_memory_id=memory_id,
            source_session_id=session_id or existing.source_session_id,
            actor=actor,
            reason=note,
            privacy_boundary=privacy_boundary,
        )
    if normalized_action == "pin":
        return await pin_memory(
            memory_id=memory_id,
            actor=actor,
            reason=note,
            privacy_boundary=privacy_boundary,
        )
    if normalized_action == "forget":
        return await forget_memory(
            memory_id=memory_id,
            actor=actor,
            reason=note,
            privacy_boundary=privacy_boundary,
        )
    if normalized_action == "audit":
        return await audit_memory(memory_id=memory_id, actor=actor, reason=note)
    raise ValueError("action must be one of: correct, pin, forget, audit")


async def list_memory_audit_receipts(
    *,
    memory_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    events = await audit_repository.list_events(limit=max(limit, 1) * 3)
    filtered: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if not (event_type.startswith("memory_") or event_type.startswith("memory.")):
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        if memory_id and details.get("memory_id") != memory_id and details.get("corrected_memory_id") != memory_id:
            continue
        filtered.append(event)
        if len(filtered) >= limit:
            break
    return {
        "events": filtered,
        "summary": {
            "event_count": len(filtered),
            "memory_id": memory_id,
            "audit_surface": "operator_memory_control",
        },
        "policy": memory_operator_policy_payload(),
    }


def _apply_provider_quarantine_overlay(inventory: dict[str, Any]) -> dict[str, Any]:
    providers = []
    for item in inventory.get("providers", []):
        if not isinstance(item, dict):
            continue
        provider = dict(item)
        quarantine = _PROVIDER_QUARANTINES.get(str(provider.get("name") or ""))
        if quarantine:
            provider["runtime_state_before_quarantine"] = provider.get("runtime_state")
            provider["runtime_state"] = "quarantined"
            provider["quarantine"] = dict(quarantine)
            notes = list(provider.get("notes") if isinstance(provider.get("notes"), list) else [])
            notes.append("Operator quarantine is active; provider remains advisory and blocked from live controls.")
            provider["notes"] = notes
        providers.append(provider)

    summary = dict(inventory.get("summary") if isinstance(inventory.get("summary"), dict) else {})
    summary["quarantined_count"] = sum(
        1 for item in providers if str(item.get("runtime_state") or "") == "quarantined"
    )
    return {
        **inventory,
        "providers": providers,
        "summary": summary,
        "provider_runtime_controls": {
            "quarantined_providers": sorted(_PROVIDER_QUARANTINES),
            "state_scope": "runtime_process_memory",
            "persistent_provider_state_available": False,
        },
    }


def _candidate_payload(memory: Memory, *, candidate_type: str) -> dict[str, Any]:
    payload = _memory_payload(memory)
    metadata = payload["metadata"] if isinstance(payload.get("metadata"), dict) else {}
    payload["candidate_type"] = candidate_type
    payload["review_state"] = str(
        (metadata.get("operator_control") or {}).get("review_outcome")
        or metadata.get("review_state")
        or "unreviewed"
    )
    payload["evidence_state"] = str(
        metadata.get("archived_reason")
        or metadata.get("superseded_reason")
        or ("decayed" if metadata.get("decay_step") else "active")
    )
    return payload


def _live_memory_candidate_payload(memory: Memory) -> dict[str, Any]:
    payload = _candidate_payload(memory, candidate_type="guardian_memory_live_control")
    metadata = payload["metadata"] if isinstance(payload.get("metadata"), dict) else {}
    operator_control = metadata.get("operator_control") if isinstance(metadata.get("operator_control"), dict) else {}
    evidence_state = str(payload.get("evidence_state") or "active")
    delete_export_state = str(
        operator_control.get("delete_export_state")
        or metadata.get("delete_export_state")
        or ("completed" if metadata.get("archived_reason") == "operator_delete_export" else "not_requested")
    )
    rollback_available = payload["status"] in {"archived", "superseded"} or bool(operator_control.get("rollback_state"))
    stale_evidence = evidence_state not in {"active", "decayed"} or bool(metadata.get("decay_step"))
    recommended_actions = ["review_outcome"]
    if stale_evidence:
        recommended_actions.append("decay_stale_evidence")
    if rollback_available:
        recommended_actions.append("rollback_memory")
    if delete_export_state in {"pending", "pending_review", "not_requested"}:
        recommended_actions.append("propagate_delete_export")
    return {
        "id": payload["id"],
        "kind": payload["kind"],
        "status": payload["status"],
        "summary": payload["summary"] or "",
        "content": payload["content"] if payload["privacy_boundary"] == "operator_visible" else "[redacted by privacy boundary]",
        "confidence": payload["confidence"],
        "privacy_boundary": payload["privacy_boundary"],
        "learning_outcome": str(operator_control.get("review_outcome") or payload.get("review_state") or "unreviewed"),
        "stale_evidence": stale_evidence,
        "rollback_available": rollback_available,
        "delete_export_state": delete_export_state,
        "recommended_actions": recommended_actions,
    }


def _scope_memories_to_owner(memories: list[Memory], owner_session_id: str | None) -> list[Memory]:
    normalized_owner = str(owner_session_id or "").strip()
    if not normalized_owner:
        return memories
    return [memory for memory in memories if memory.source_session_id == normalized_owner]


def _live_provider_control_payload(provider: dict[str, Any]) -> dict[str, Any]:
    name = str(provider.get("name") or "")
    runtime_state = str(provider.get("runtime_state") or "unknown")
    quarantine = provider.get("quarantine") if isinstance(provider.get("quarantine"), dict) else {}
    control_state = str(quarantine.get("state") or ("active" if runtime_state == "ready" else "watch"))
    retrieval_allowed = runtime_state in {"ready", "degraded"} and control_state != "quarantined"
    governance = provider.get("governance") if isinstance(provider.get("governance"), dict) else {}
    writeback_state = str(governance.get("writeback_state") or "undeclared")
    notes = provider.get("notes")
    note_list = [str(item) for item in notes] if isinstance(notes, (list, tuple)) else []
    return {
        "name": name,
        "runtime_state": runtime_state,
        "control_state": control_state,
        "retrieval_allowed": retrieval_allowed,
        "writeback_allowed": retrieval_allowed and writeback_state == "ready",
        "advisory_only": True,
        "notes": note_list,
        "recommended_actions": ["reinstate_provider"] if control_state == "quarantined" else ["quarantine_provider"],
    }


def _live_action_receipt_payload(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    target_id = str(details.get("memory_id") or details.get("provider_name") or "")
    return {
        "id": str(event.get("id") or event.get("event_id") or f"receipt-{target_id}"),
        "action": str(details.get("action") or event.get("event_type") or "memory_live_control").replace("memory_live_control_", ""),
        "target_kind": "provider" if details.get("provider_name") else "memory",
        "target_id": target_id,
        "summary": str(event.get("summary") or ""),
        "outcome": str(details.get("outcome") or details.get("review_outcome") or "recorded"),
        "changed_memory": bool(details.get("changed_memory")),
        "changed_provider_state": bool(details.get("changed_provider")),
        "risk_level": str(event.get("risk_level") or "low"),
        "created_at": str(event.get("created_at") or ""),
    }


async def get_memory_live_controls_snapshot(
    *,
    limit: int = 8,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    bounded_limit = min(max(int(limit or 8), 1), 50)
    provider_inventory = _apply_provider_quarantine_overlay(list_memory_provider_inventory())
    fetch_limit = bounded_limit if not owner_session_id else min(bounded_limit * 10, 200)
    active = _scope_memories_to_owner(
        await memory_repository.list_memories(status=MemoryStatus.active, limit=fetch_limit),
        owner_session_id,
    )[:bounded_limit]
    superseded = _scope_memories_to_owner(
        await memory_repository.list_memories(status=MemoryStatus.superseded, limit=fetch_limit),
        owner_session_id,
    )[:bounded_limit]
    archived = _scope_memories_to_owner(
        await memory_repository.list_memories(status=MemoryStatus.archived, limit=fetch_limit),
        owner_session_id,
    )[:bounded_limit]
    receipts = await list_memory_audit_receipts(limit=bounded_limit)
    reconciliation = await summarize_memory_reconciliation_state(limit=min(bounded_limit, 10))

    active_candidates = [_candidate_payload(memory, candidate_type="memory_candidate") for memory in active]
    review_candidates = [
        *[_candidate_payload(memory, candidate_type="superseded_review_candidate") for memory in superseded],
        *[_candidate_payload(memory, candidate_type="archived_review_candidate") for memory in archived],
    ][:bounded_limit]
    live_candidates = [
        *[_live_memory_candidate_payload(memory) for memory in active],
        *[_live_memory_candidate_payload(memory) for memory in superseded],
        *[_live_memory_candidate_payload(memory) for memory in archived],
    ][:bounded_limit]
    provider_controls = [
        _live_provider_control_payload(provider)
        for provider in provider_inventory.get("providers", [])
        if isinstance(provider, dict)
    ]
    action_receipts = [
        _live_action_receipt_payload(event)
        for event in receipts.get("events", [])
        if str(event.get("event_type") or "").startswith("memory_live_control_")
    ][:bounded_limit]
    delete_export_pending_count = sum(
        1 for candidate in live_candidates
        if candidate["delete_export_state"] in {"pending", "pending_review"}
    )
    snapshot = {
        "summary": {
            "operator_status": "guardian_memory_live_controls_visible",
            "provider_count": provider_inventory.get("summary", {}).get("provider_count", 0),
            "quarantined_provider_count": provider_inventory.get("summary", {}).get("quarantined_count", 0),
            "active_memory_candidate_count": len(active),
            "review_candidate_count": len(superseded) + len(archived),
            "recent_receipt_count": len(receipts.get("events", [])),
            "memory_candidate_count": len(live_candidates),
            "stale_candidate_count": sum(1 for candidate in live_candidates if candidate["stale_evidence"]),
            "rollback_available_count": sum(1 for candidate in live_candidates if candidate["rollback_available"]),
            "delete_export_pending_count": delete_export_pending_count,
            "action_receipt_count": len(action_receipts),
            "owner_session_id": owner_session_id,
            "claim_boundary": "live_operator_controls_only",
        },
        "provider_states": provider_inventory,
        "learning_memory_candidates": {
            "active": active_candidates,
            "review": review_candidates,
        },
        "recent_receipts": receipts.get("events", []),
        "memory_candidates": live_candidates,
        "provider_controls": provider_controls,
        "action_receipts": action_receipts,
        "reconciliation": reconciliation,
        "blocked_claims": list(_BLOCKED_LIVE_CONTROL_CLAIMS),
        "policy": memory_operator_policy_payload(),
    }
    return snapshot


def _require_acknowledged(acknowledged: bool) -> None:
    if acknowledged is not True:
        raise ValueError("explicit acknowledgement is required for memory live-control actions")


async def _log_live_control_event(
    *,
    actor: str,
    action: str,
    summary: str,
    privacy_boundary: str,
    session_id: str | None = None,
    details: dict[str, Any] | None = None,
):
    return await audit_repository.log_event(
        actor=actor,
        event_type=f"memory_live_control_{action}",
        tool_name="memory_live_control",
        risk_level="medium" if privacy_boundary in {"sensitive", "source_bound"} else "low",
        policy_mode="operator_controlled",
        session_id=session_id,
        summary=summary,
        details={
            "action": action,
            "privacy_boundary": privacy_boundary,
            "acknowledged": True,
            "claim_boundary": "operator_live_control_receipt_not_superiority_or_full_parity_claim",
            "blocked_claims": list(_BLOCKED_LIVE_CONTROL_CLAIMS),
            **(details or {}),
        },
    )


async def apply_memory_live_control_action(
    *,
    action: str,
    acknowledged: bool,
    actor: str = "operator",
    reason: str | None = None,
    owner_session_id: str | None = None,
    memory_id: str | None = None,
    provider_name: str | None = None,
    outcome: str | None = None,
    privacy_boundary: str | None = None,
) -> dict[str, Any]:
    _require_acknowledged(acknowledged)
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _LIVE_CONTROL_ACTIONS:
        raise ValueError(
            "action must be one of: " + ", ".join(sorted(_LIVE_CONTROL_ACTIONS))
        )
    boundary = _normalize_privacy_boundary(privacy_boundary)
    now = _now()
    memory: Memory | None = None
    changed_memory = False
    changed_provider = False
    result: dict[str, Any] = {}

    if normalized_action == "review_outcome":
        if not memory_id:
            raise ValueError("memory_id is required for review_outcome")
        normalized_outcome = _REVIEW_OUTCOME_ALIASES.get(str(outcome or "").strip().lower())
        if normalized_outcome is None:
            raise ValueError("outcome must be one of: accepted, helpful, rejected, harmful, ignored, corrected, needs_follow_up")
        update_kwargs: dict[str, Any] = {
            "metadata_updates": _operator_metadata(
                action="review_outcome",
                actor=actor,
                privacy_boundary=boundary,
                reason=reason,
                extra={"review_outcome": normalized_outcome},
            ),
            "last_confirmed_at": now if normalized_outcome == "accepted" else None,
        }
        if normalized_outcome == "accepted":
            update_kwargs.update({"status": MemoryStatus.active, "confidence": 1.0, "importance": 1.0})
        elif normalized_outcome == "rejected":
            update_kwargs.update({"status": MemoryStatus.archived, "confidence": 0.0, "importance": 0.0})
        memory = await memory_repository.update_memory_control_metadata(memory_id, **update_kwargs)
        changed_memory = True
        result["review_outcome"] = normalized_outcome
    elif normalized_action == "decay_stale_evidence":
        if memory_id:
            existing = await memory_repository.get_memory(memory_id)
            if existing is None:
                raise ValueError(f"Unknown memory id: {memory_id}")
            memory = await memory_repository.update_memory_control_metadata(
                memory_id,
                confidence=max(0.0, min(float(existing.confidence or 0.0), 0.5)),
                importance=max(0.0, min(float(existing.importance or 0.0), 0.5)),
                reinforcement=max(0.0, float(existing.reinforcement or 0.0) * 0.5),
                metadata_updates={
                    **_operator_metadata(
                        action="decay_stale_evidence",
                        actor=actor,
                        privacy_boundary=boundary,
                        reason=reason,
                        extra={"stale_evidence_state": "operator_decayed"},
                    ),
                    "decay_step": "operator_live_control",
                    "decayed_at": now.isoformat(),
                },
            )
            changed_memory = True
            result["decay"] = {
                "target_scope": "memory",
                "memory_id": memory.id,
                "decayed_count": 1,
                "global_decay_ran": False,
            }
        else:
            decay_result = await apply_memory_decay_policies(now=now)
            result["decay"] = {**asdict(decay_result), "target_scope": "global", "global_decay_ran": True}
    elif normalized_action == "rollback_memory":
        if not memory_id:
            raise ValueError("memory_id is required for rollback_memory")
        existing = await memory_repository.get_memory(memory_id)
        if existing is None:
            raise ValueError(f"Unknown memory id: {memory_id}")
        memory = await memory_repository.update_memory_control_metadata(
            memory_id,
            status=MemoryStatus.active,
            confidence=max(float(existing.confidence or 0.0), 0.55),
            importance=max(float(existing.importance or 0.0), 0.55),
            reinforcement=max(float(existing.reinforcement or 0.0), 1.0),
            last_confirmed_at=now,
            metadata_updates=_operator_metadata(
                action="rollback_memory",
                actor=actor,
                privacy_boundary=boundary,
                reason=reason,
                extra={"rollback_state": "active_reinstated"},
            ),
        )
        changed_memory = True
    elif normalized_action == "propagate_delete_export":
        if not memory_id:
            raise ValueError("memory_id is required for propagate_delete_export")
        memory = await memory_repository.update_memory_control_metadata(
            memory_id,
            status=MemoryStatus.archived,
            content="[delete/export propagated by operator]",
            summary="[delete/export propagated by operator]",
            confidence=0.0,
            importance=0.0,
            reinforcement=0.0,
            metadata_updates={
                **_operator_metadata(
                    action="propagate_delete_export",
                    actor=actor,
                    privacy_boundary=boundary,
                    reason=reason,
                    extra={
                        "delete_export_state": "canonical_memory_redacted",
                        "provider_propagation_state": "runtime_receipt_only_no_full_provider_parity_claim",
                    },
                ),
                "archived_reason": "operator_delete_export",
                "archived_at": now.isoformat(),
            },
        )
        changed_memory = True
    elif normalized_action in {"quarantine_provider", "reinstate_provider"}:
        normalized_provider = str(provider_name or "").strip()
        if not normalized_provider:
            raise ValueError("provider_name is required for provider controls")
        inventory = list_memory_provider_inventory()
        provider_names = {
            str(item.get("name") or "")
            for item in inventory.get("providers", [])
            if isinstance(item, dict)
        }
        if normalized_provider not in provider_names and normalized_provider not in _PROVIDER_QUARANTINES:
            raise ValueError(f"Unknown memory provider: {normalized_provider}")
        if normalized_action == "quarantine_provider":
            _PROVIDER_QUARANTINES[normalized_provider] = {
                "state": "quarantined",
                "actor": actor,
                "reason": str(reason or "").strip(),
                "quarantined_at": now.isoformat(),
                "privacy_boundary": boundary,
            }
        else:
            _PROVIDER_QUARANTINES.pop(normalized_provider, None)
        changed_provider = True
        result["provider_state"] = (
            _PROVIDER_QUARANTINES.get(normalized_provider)
            or {"state": "reinstated", "provider_name": normalized_provider}
        )

    audit_event = await _log_live_control_event(
        actor=actor,
        action=normalized_action,
        summary=f"Operator applied memory live control: {normalized_action}",
        privacy_boundary=boundary,
        session_id=memory.source_session_id if memory is not None else owner_session_id,
        details={
            "memory_id": memory_id,
            "provider_name": provider_name,
            "owner_session_id": owner_session_id,
            "outcome": outcome,
            "changed_memory": changed_memory,
            "changed_provider": changed_provider,
            **result,
        },
    )
    receipt = {
        "action": normalized_action,
        "actor": actor,
        "acknowledged": True,
        "changed_memory": changed_memory,
        "changed_provider": changed_provider,
        "memory_id": memory.id if memory is not None else memory_id,
        "provider_name": provider_name,
        "owner_session_id": owner_session_id,
        "privacy_boundary": boundary,
        "audit_event_type": f"memory_live_control_{normalized_action}",
        "audit_event_id": audit_event.id,
        "claim_boundary": "operator_live_control_receipt_not_solved_learning_superiority_or_full_parity_claim",
        "blocked_claims": list(_BLOCKED_LIVE_CONTROL_CLAIMS),
        **result,
    }
    return {
        "memory": _memory_payload(memory) if memory is not None else None,
        "receipt": receipt,
        "snapshot": await get_memory_live_controls_snapshot(limit=8),
        "policy": memory_operator_policy_payload(),
    }
