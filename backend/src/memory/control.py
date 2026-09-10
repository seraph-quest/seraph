from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from src.audit.repository import audit_repository
from src.db.engine import get_session
from src.db.models import Memory, MemoryEdgeType, MemoryKind, MemoryStatus, StrategyDelta
from src.memory.decay import apply_memory_decay_policies, summarize_memory_reconciliation_state
from src.memory.providers import list_memory_provider_inventory
from src.memory.repository import (
    _CANONICAL_MEMORY_DELETE_EXPORT_REASON,
    _CANONICAL_MEMORY_DELETE_CONTENT,
    _CANONICAL_MEMORY_REDACTED_STATE,
    _canonical_memory_deletion_marker,
    _recovery_authority,
    memory_repository,
)
from src.memory.snapshots import invalidate_bounded_guardian_snapshot_cache
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


@dataclass(frozen=True)
class StrategyDeltaReceipt:
    """Operator-visible receipt for a reversible goal strategy change."""

    delta_id: str
    goal_id: str
    scope: str
    field_name: str
    before: dict[str, Any]
    after: dict[str, Any]
    source_event_id: str
    author_id: str
    evaluator_id: str | None
    goal_revision_before: int
    goal_revision_after: int | None
    status: str
    rollback_target_id: str | None
    reason: str
    created_at: str
    updated_at: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def _strategy_delta_payload(delta: StrategyDelta) -> StrategyDeltaReceipt:
    def _decode(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    return StrategyDeltaReceipt(
        delta_id=delta.delta_id,
        goal_id=delta.goal_id,
        scope=delta.scope,
        field_name=delta.field_name,
        before=_decode(delta.before_json),
        after=_decode(delta.after_json),
        source_event_id=delta.source_event_id,
        author_id=delta.author_id,
        evaluator_id=delta.evaluator_id,
        goal_revision_before=delta.goal_revision_before,
        goal_revision_after=delta.goal_revision_after,
        status=delta.status,
        rollback_target_id=delta.rollback_target_id,
        reason=delta.reason,
        created_at=delta.created_at.isoformat(),
        updated_at=delta.updated_at.isoformat(),
    )


async def record_strategy_delta_proposal(
    *,
    goal_id: str,
    source_event_id: str,
    before: dict[str, Any],
    after: dict[str, Any],
    author_id: str,
    goal_revision_before: int,
    reason: str,
    delta_id: str | None = None,
    scope: str = "goal",
    field_name: str = "web_brief_target",
) -> StrategyDeltaReceipt:
    """Create or replay one bounded operator strategy correction."""

    if not goal_id.strip() or not source_event_id.strip() or not author_id.strip():
        raise ValueError("strategy delta identity is required")
    if len(source_event_id.strip()) > 160:
        raise ValueError("strategy delta source_event_id is too long")
    if delta_id is not None and (not delta_id.strip() or len(delta_id.strip()) > 128):
        raise ValueError("strategy delta delta_id is invalid")
    if len(json.dumps(before, sort_keys=True, separators=(",", ":"))) > 8_192:
        raise ValueError("strategy delta before state is too large")
    if len(json.dumps(after, sort_keys=True, separators=(",", ":"))) > 8_192:
        raise ValueError("strategy delta after state is too large")
    try:
        async with get_session() as db:
            existing_result = await db.execute(
                select(StrategyDelta).where(StrategyDelta.source_event_id == source_event_id)
            )
            existing = existing_result.scalars().first()
            if existing is not None:
                return _strategy_delta_payload(existing)
            delta = StrategyDelta(
                **({"delta_id": delta_id.strip()} if delta_id else {}),
                goal_id=goal_id,
                scope=scope,
                field_name=field_name,
                before_json=json.dumps(before, sort_keys=True, separators=(",", ":")),
                after_json=json.dumps(after, sort_keys=True, separators=(",", ":")),
                source_event_id=source_event_id,
                author_id=author_id,
                goal_revision_before=goal_revision_before,
                status="proposed",
                reason=reason.strip()[:1_000],
            )
            db.add(delta)
            await db.flush()
            db.expunge(delta)
            return _strategy_delta_payload(delta)
    except SQLAlchemyError:
        # A concurrent retry may win the unique source-event fence between the
        # read and insert.  Re-read the committed winner; surface other DB
        # failures instead of manufacturing a receipt.
        existing = await get_strategy_delta_by_source_event(source_event_id)
        if existing is not None:
            return existing
        raise


async def get_strategy_delta(delta_id: str) -> StrategyDeltaReceipt | None:
    async with get_session() as db:
        result = await db.execute(select(StrategyDelta).where(StrategyDelta.delta_id == delta_id))
        delta = result.scalars().first()
        return _strategy_delta_payload(delta) if delta is not None else None


async def get_strategy_delta_by_source_event(source_event_id: str) -> StrategyDeltaReceipt | None:
    async with get_session() as db:
        result = await db.execute(
            select(StrategyDelta).where(StrategyDelta.source_event_id == source_event_id)
        )
        delta = result.scalars().first()
        return _strategy_delta_payload(delta) if delta is not None else None


async def list_strategy_deltas(
    goal_id: str,
    *,
    limit: int = 50,
) -> list[StrategyDeltaReceipt]:
    """Return bounded, newest-first strategy corrections for operator inspection."""

    bounded_limit = min(max(int(limit), 1), 200)
    async with get_session() as db:
        result = await db.execute(
            select(StrategyDelta)
            .where(StrategyDelta.goal_id == goal_id)
            .order_by(StrategyDelta.created_at.desc())
            .limit(bounded_limit)
        )
        return [_strategy_delta_payload(delta) for delta in result.scalars().all()]


async def update_strategy_delta(
    delta_id: str,
    *,
    status: str,
    goal_revision_after: int | None = None,
    rollback_target_id: str | None = None,
    expected_status: str | tuple[str, ...] | None = None,
) -> StrategyDeltaReceipt | None:
    if status not in {"proposed", "applied", "rolled_back", "rejected"}:
        raise ValueError("invalid strategy delta status")
    async with get_session() as db:
        guards = [StrategyDelta.delta_id == delta_id]
        if expected_status is not None:
            statuses = (expected_status,) if isinstance(expected_status, str) else expected_status
            guards.append(StrategyDelta.status.in_(statuses))
        result = await db.execute(
            update(StrategyDelta)
            .where(*guards)
            .values(
                status=status,
                goal_revision_after=goal_revision_after,
                rollback_target_id=rollback_target_id,
                updated_at=_now(),
            )
        )
        if result.rowcount != 1:
            current_result = await db.execute(
                select(StrategyDelta).where(StrategyDelta.delta_id == delta_id)
            )
            current = current_result.scalars().first()
            return _strategy_delta_payload(current) if current is not None else None
        refreshed_result = await db.execute(
            select(StrategyDelta).where(StrategyDelta.delta_id == delta_id)
        )
        refreshed = refreshed_result.scalars().first()
        if refreshed is None:
            return None
        db.expunge(refreshed)
        return _strategy_delta_payload(refreshed)


def memory_operator_policy_payload() -> dict[str, Any]:
    return {
        "authoritative_memory": "guardian",
        "operator_authority": "operator_corrections_override_agent_extraction",
        "control_primitives": [
            "correct",
            "pin",
            "forget",
            "audit",
            "export",
            "rebuild",
            "restore",
            *sorted(_LIVE_CONTROL_ACTIONS),
        ],
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
        "recovery_authority": {
            "owner_binding": "authenticated_operator_session",
            "accepted_source_role": "operator",
            "body_actor_is_ignored": True,
            "tombstone_precedence": "current_ledger_beats_older_archive",
            "derived_index_mode": "deterministic_canonical_lexical",
        },
        "recovery_states": ["ready", "degraded_no_learning", "blocked"],
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
    authenticated_session_id: str | None = None,
    source_role: str = "operator",
) -> dict[str, Any]:
    normalized_source_role = str(source_role or "").strip().lower()
    if normalized_source_role != "operator":
        raise PermissionError("memory correction source role must be operator")
    if authenticated_session_id is not None:
        normalized_authenticated_session = str(authenticated_session_id).strip()
        normalized_requested_session = str(source_session_id or "").strip()
        if normalized_requested_session and normalized_requested_session != normalized_authenticated_session:
            raise PermissionError("memory correction source session does not match the authenticated session")
        source_session_id = normalized_authenticated_session
        if corrects_memory_id:
            existing_target = await memory_repository.get_memory(corrects_memory_id)
            if existing_target is None:
                raise ValueError(f"Unknown memory id: {corrects_memory_id}")
            target_session_id = str(existing_target.source_session_id or "").strip()
            if target_session_id and target_session_id != normalized_authenticated_session:
                raise PermissionError("memory correction target belongs to another owner session")
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
    superseded_metadata_updates = {
        "superseded_reason": "operator_correction",
        "operator_control": {
            "last_action": "superseded_by_operator_correction",
            "last_actor": actor,
            "last_reason": str(reason or "").strip(),
            "last_action_at": now.isoformat(),
        },
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
        supersedes_memory_id=corrects_memory_id,
        supersedes_metadata=superseded_metadata_updates if corrects_memory_id else None,
    )
    memory = await memory_repository.get_memory(created.memory_id)
    if memory is None:  # pragma: no cover - defensive, create_memory already flushed
        raise ValueError(f"Unknown memory id: {created.memory_id}")

    corrected_memory = None
    if corrects_memory_id:
        corrected_memory = await memory_repository.get_memory(corrects_memory_id)
        if corrected_memory is None:  # pragma: no cover - atomic target update
            raise ValueError(f"Unknown memory id: {corrects_memory_id}")
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


async def export_memory_recovery(
    *,
    actor: str,
    owner_session_id: str,
    authenticated_session_id: str,
    source_role: str = "operator",
    limit: int = 10_000,
) -> dict[str, Any]:
    """Export canonical memory through the authenticated recovery boundary."""

    result = await memory_repository.export_canonical_memory_state(
        actor=actor,
        owner_session_id=owner_session_id,
        authenticated_session_id=authenticated_session_id,
        source_role=source_role,
        limit=limit,
    )
    if result.get("status") == "ready":
        event = await audit_repository.log_event(
            actor=actor,
            event_type="memory_recovery_exported",
            tool_name="memory_recovery",
            risk_level="medium",
            policy_mode="authenticated_operator",
            session_id=owner_session_id,
            summary="Authenticated operator exported canonical memory",
            details={
                "artifact_path": result.get("artifact_path"),
                "artifact_sha256": result.get("artifact_sha256"),
                "export_hash": result.get("export_hash"),
                "counts": result.get("counts"),
                "memory_ids": result.get("memory_ids"),
                "tombstone_ids": result.get("tombstone_ids"),
                "source_role": source_role,
            },
        )
        result["audit_event_id"] = event.id
    return result


async def rebuild_memory_recovery(
    *,
    actor: str,
    owner_session_id: str,
    authenticated_session_id: str,
    source_role: str = "operator",
    limit: int = 10_000,
) -> dict[str, Any]:
    """Rebuild the local derived memory index without invoking a provider."""

    result = await memory_repository.rebuild_canonical_memory_index(
        actor=actor,
        owner_session_id=owner_session_id,
        authenticated_session_id=authenticated_session_id,
        source_role=source_role,
        limit=limit,
    )
    if result.get("status") == "ready":
        event = await audit_repository.log_event(
            actor=actor,
            event_type="memory_recovery_rebuilt",
            tool_name="memory_recovery",
            risk_level="low",
            policy_mode="authenticated_operator",
            session_id=owner_session_id,
            summary="Authenticated operator rebuilt local memory index",
            details={
                "artifact_path": result.get("artifact_path"),
                "artifact_sha256": result.get("artifact_sha256"),
                "index_hash": result.get("index_hash"),
                "memory_ids": result.get("memory_ids"),
                "semantic_index_status": result.get("semantic_index_status"),
                "source_role": source_role,
            },
        )
        result["audit_event_id"] = event.id
    return result


async def restore_memory_recovery(
    *,
    archive: dict[str, Any],
    actor: str,
    owner_session_id: str,
    authenticated_session_id: str,
    source_role: str = "operator",
) -> dict[str, Any]:
    """Restore an archive while preserving current canonical tombstones."""

    result = await memory_repository.restore_canonical_memory_state(
        archive,
        actor=actor,
        owner_session_id=owner_session_id,
        authenticated_session_id=authenticated_session_id,
        source_role=source_role,
    )
    event = await audit_repository.log_event(
        actor=actor,
        event_type="memory_recovery_restored",
        tool_name="memory_recovery",
        risk_level="medium",
        policy_mode="authenticated_operator",
        session_id=owner_session_id,
        summary="Authenticated operator restored canonical memory archive",
        details={
            "archive_hash": result.get("archive_hash"),
            "restored_memory_ids": result.get("restored_memory_ids"),
            "tombstone_suppressed_memory_ids": result.get("tombstone_suppressed_memory_ids"),
            "newer_conflict_memory_ids": result.get("newer_conflict_memory_ids"),
            "reconciliation": result.get("reconciliation"),
            "source_role": source_role,
        },
    )
    result["audit_event_id"] = event.id
    return result


async def memory_recovery_status(
    *,
    owner_session_id: str,
    authenticated_session_id: str,
    actor: str,
    source_role: str = "operator",
) -> dict[str, Any]:
    """Return operator-visible canonical recovery and no-learning state."""

    normalized_actor, normalized_owner = _recovery_authority(
        actor=actor,
        owner_session_id=owner_session_id,
        authenticated_session_id=authenticated_session_id,
        source_role=source_role,
    )
    try:
        reconciliation = await memory_repository.reconcile_memory_tombstones(
            owner_session_id=normalized_owner,
        )
        revision = await memory_repository.get_memory_tombstone_revision(
            owner_session_id=normalized_owner,
        )
    except SQLAlchemyError:
        return {
            "schema_version": "guardian.memory.recovery_status.v1",
            "status": "degraded_no_learning",
            "operator_status": "canonical_memory_recovery_unavailable",
            "no_learning_reason": "canonical memory database unavailable",
            "owner_session_id": normalized_owner,
            "provenance": {"kind": "operator_memory_recovery_status", "actor": normalized_actor},
            "reconciliation": {"status": "unavailable"},
            "canonical_tombstone_revision": None,
            "retrieval_mode": "disabled_until_canonical_recovery",
        }
    ready = reconciliation.get("status") == "ready"
    return {
        "schema_version": "guardian.memory.recovery_status.v1",
        "status": "ready" if ready else "degraded_no_learning",
        "operator_status": "canonical_memory_recovery_ready" if ready else "canonical_memory_recovery_degraded",
        "no_learning_reason": None if ready else "canonical tombstone ledger requires repair",
        "owner_session_id": normalized_owner,
        "provenance": {
            "kind": "operator_memory_recovery_status",
            "actor": normalized_actor,
            "source_role": source_role,
        },
        "reconciliation": reconciliation,
        "canonical_tombstone_revision": revision,
        "retrieval_mode": "deterministic_canonical_lexical" if ready else "disabled_until_canonical_recovery",
        "semantic_index_status": "unavailable",
        "provider_calls": 0,
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
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    normalized_owner = str(owner_session_id or "").strip() or None
    events = await audit_repository.list_events(
        limit=max(limit, 1) * 3,
        session_id=normalized_owner,
    )
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
            "owner_session_id": normalized_owner,
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


async def _verify_live_control_memory_owner(
    *,
    memory_id: str,
    owner_session_id: str,
) -> None:
    """Fence a direct live-control call to the authenticated memory owner."""

    normalized_memory_id = str(memory_id or "").strip()
    normalized_owner = str(owner_session_id or "").strip()
    if not normalized_memory_id or not normalized_owner:
        return
    memory = await memory_repository.get_memory(
        normalized_memory_id,
        include_deleted=True,
    )
    if memory is None:
        raise ValueError(f"Unknown memory id: {normalized_memory_id}")
    bound_owner = str(memory.source_session_id or "").strip()
    if not bound_owner:
        raise PermissionError(
            f"memory {normalized_memory_id} has no owner session"
        )
    if bound_owner != normalized_owner:
        raise PermissionError(
            f"memory {normalized_memory_id} belongs to another owner session"
        )


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
    try:
        tombstone_reconciliation = await memory_repository.reconcile_memory_tombstones(
            owner_session_id=owner_session_id,
        )
        if tombstone_reconciliation.get("status") != "ready":
            active = []
            superseded = []
            archived = []
            receipts = {"events": []}
            reconciliation = {
                "summary": {
                    "status": "degraded_no_learning",
                    "reason": "canonical tombstone reconciliation requires repair",
                },
                "tombstone_reconciliation": tombstone_reconciliation,
            }
            operator_status = "guardian_memory_live_controls_degraded"
        else:
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
            receipts = await list_memory_audit_receipts(
                limit=bounded_limit,
                owner_session_id=owner_session_id,
            )
            reconciliation = await summarize_memory_reconciliation_state(
                limit=min(bounded_limit, 10),
                owner_session_id=owner_session_id,
                content_free=owner_session_id is not None,
            )
            operator_status = "guardian_memory_live_controls_visible"
    except SQLAlchemyError:
        active = []
        superseded = []
        archived = []
        receipts = {"events": []}
        reconciliation = {
            "summary": {
                "status": "unavailable",
                "reason": "memory database unavailable",
            },
            "items": [],
        }
        operator_status = "guardian_memory_live_controls_degraded"

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
            "operator_status": operator_status,
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
    authenticated_session_id: str | None = None,
    source_role: str = "operator",
) -> dict[str, Any]:
    if authenticated_session_id is not None:
        # API routes pass the middleware-bound session here.  Keep direct
        # internal calls backwards-compatible, while making every externally
        # reachable live-control route prove the same runtime authority as
        # canonical recovery.
        _recovery_authority(
            actor=actor,
            owner_session_id=owner_session_id,
            authenticated_session_id=authenticated_session_id,
            source_role=source_role,
        )
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

    if memory_id and owner_session_id:
        await _verify_live_control_memory_owner(
            memory_id=memory_id,
            owner_session_id=owner_session_id,
        )

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
        deletion_marker = _canonical_memory_deletion_marker(existing)
        if deletion_marker is not None:
            raise ValueError(
                "cannot rollback canonical memory after operator delete/export "
                f"redaction ({deletion_marker})"
            )
        memory = await memory_repository.rollback_memory_if_unchanged(
            memory_id,
            expected_updated_at=existing.updated_at,
            expected_metadata_json=existing.metadata_json,
            expected_status=existing.status,
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
        tombstone_result = await memory_repository.mark_memory_tombstoned(
            memory_id,
            actor=actor,
            reason=reason,
            metadata_updates={
                **_operator_metadata(
                    action="propagate_delete_export",
                    actor=actor,
                    privacy_boundary=boundary,
                    reason=reason,
                    extra={
                        "delete_export_state": _CANONICAL_MEMORY_REDACTED_STATE,
                        "provider_propagation_state": "runtime_receipt_only_no_full_provider_parity_claim",
                    },
                ),
                "archived_reason": _CANONICAL_MEMORY_DELETE_EXPORT_REASON,
                "archived_at": now.isoformat(),
            },
        )
        memory = tombstone_result.memory
        result["tombstone"] = {
            "id": tombstone_result.tombstone.id,
            "memory_id": tombstone_result.tombstone.memory_id,
            "actor": tombstone_result.tombstone.actor,
            "reason": tombstone_result.tombstone.reason,
            "created_at": tombstone_result.tombstone.created_at.isoformat(),
            "created": tombstone_result.created,
            "state": _CANONICAL_MEMORY_REDACTED_STATE,
        }
        invalidate_bounded_guardian_snapshot_cache()
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
        "snapshot": await get_memory_live_controls_snapshot(
            limit=8,
            owner_session_id=owner_session_id,
        ),
        "policy": memory_operator_policy_payload(),
    }
