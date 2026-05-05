from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.audit.repository import audit_repository
from src.db.models import Memory, MemoryEdgeType, MemoryKind, MemoryStatus
from src.memory.repository import memory_repository
from src.memory.types import kind_to_category, normalize_memory_kind


_PRIVACY_BOUNDARIES = {
    "operator_visible",
    "private",
    "sensitive",
    "source_bound",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_score(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return default


def _normalize_privacy_boundary(value: str | None) -> str:
    normalized = str(value or "operator_visible").strip().lower()
    return normalized if normalized in _PRIVACY_BOUNDARIES else "operator_visible"


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
        "privacy_boundary": metadata.get("privacy_boundary") or "operator_visible",
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
        "control_primitives": ["correct", "pin", "forget", "audit"],
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
        **_operator_metadata(
            action="correction",
            actor=actor,
            privacy_boundary=boundary,
            reason=reason,
            extra={"pinned": False},
        ),
        **(metadata or {}),
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
