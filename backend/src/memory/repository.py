from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings
from sqlalchemy import exists, func, or_, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import (
    AuditEvent,
    Memory,
    MemoryCategory,
    MemoryEdge,
    MemoryEdgeType,
    MemoryEpisode,
    MemoryEpisodeType,
    MemoryEntity,
    MemoryEntityType,
    MemoryKind,
    MemorySnapshot,
    MemorySnapshotKind,
    MemorySource,
    MemoryStatus,
    MemoryTombstone,
    MemoryProposal,
    MemoryProposalDecisionEffect,
    MemoryProposalProviderContactState,
    MemoryProposalPrivacyState,
    MemoryProposalStatus,
    WorkBoardDecisionReceipt,
    WorkBoardDecisionAdmissionStatus,
    WorkBoardDecisionReceiptStage,
    WorkBoardDecisionStatus,
)
from src.extensions.capability_execution import (
    CapabilityJournalError,
    _effect_mac_key,
    _mac,
)
from src.approval.runtime import get_current_session_id, get_current_trust_principal
from src.auth.cancellation import assert_runtime_not_revoked
from src.security.trust_contract import AuthorityGrant, PrincipalType
from src.workspace import (
    WorkspaceStateClass,
    WorkspaceStateError,
    canonical_workspace_registry,
    canonical_workspace_root,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_entity_key(name: str, entity_type: MemoryEntityType | str) -> str:
    normalized_type = _coerce_enum(entity_type, MemoryEntityType).value
    return f"{normalized_type}:{' '.join(name.strip().lower().split())}"


def _normalize_entity_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _name_contains_requested_tokens(candidate_name: str, requested_name: str) -> bool:
    candidate_tokens = _normalize_entity_name(candidate_name).split()
    requested_tokens = _normalize_entity_name(requested_name).split()
    if not candidate_tokens or not requested_tokens or len(requested_tokens) > len(candidate_tokens):
        return False
    for index in range(len(candidate_tokens) - len(requested_tokens) + 1):
        if candidate_tokens[index : index + len(requested_tokens)] == requested_tokens:
            return True
    return False


def _coerce_enum(
    value: Any,
    enum_cls: type[
        MemoryCategory
        | MemoryKind
        | MemoryStatus
        | MemoryEpisodeType
        | MemoryEntityType
        | MemorySnapshotKind
        | MemoryEdgeType
    ],
):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {enum_cls.__name__}: {value!r}") from exc


_CANONICAL_MEMORY_DELETE_EXPORT_REASON = "operator_delete_export"
_CANONICAL_MEMORY_REDACTED_STATE = "canonical_memory_redacted"
_CANONICAL_MEMORY_DELETE_ACTIONS = {
    "propagate_delete_export",
    "operator_delete_export",
}
_CANONICAL_MEMORY_DELETE_CONTENT = "[delete/export propagated by operator]"
_EMPTY_TOMBSTONE_REVISION = hashlib.sha256(b"[]").hexdigest()
_MEMORY_EXPORT_SCHEMA_VERSION = "guardian.memory.export.v1"
_MEMORY_INDEX_SCHEMA_VERSION = "guardian.memory.derived_index.v1"
_MAX_RECOVERY_RECORDS = 10_000
_MAX_RECOVERY_SOURCE_RECORDS = 10_000
_MAX_RECOVERY_SOURCES_PER_RECORD = 1_000


async def _begin_canonical_write(db) -> None:
    """Serialize canonical writes across tasks and processes on SQLite.

    Canonical deletion is a durable authority.  A deferred SQLite transaction
    can otherwise read a clean row and only acquire the writer lock after a
    concurrent delete has committed.  ``BEGIN IMMEDIATE`` makes the read,
    guard, and write one cross-process critical section.
    """

    await db.execute(text("BEGIN IMMEDIATE"))


async def _memory_tombstone_revision(
    db,
    *,
    owner_session_id: str | None = None,
) -> str:
    """Return a content-free revision for the durable delete ledger.

    Recovery exports use an owner-scoped revision so one operator session does
    not learn that another session's delete ledger changed.  Internal
    reconciliation callers omit the owner and retain the global authority.
    """

    statement = select(
        MemoryTombstone.id,
        MemoryTombstone.memory_id,
        MemoryTombstone.created_at,
    ).select_from(MemoryTombstone).outerjoin(
        Memory,
        Memory.id == MemoryTombstone.memory_id,
    )
    normalized_owner = str(owner_session_id or "").strip()
    if normalized_owner:
        statement = statement.where(Memory.source_session_id == normalized_owner)
    rows = (await db.execute(statement.order_by(MemoryTombstone.created_at.asc(), MemoryTombstone.id.asc()))).all()
    payload = [
        {
            "id": str(row[0]),
            "memory_id": str(row[1]),
            "created_at": _recovery_timestamp(row[2]),
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_recovery_timestamp(value: Any, *, field_name: str) -> datetime | None:
    """Parse an archive timestamp without accepting local-time ambiguity."""

    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return _normalize_utc_timestamp(parsed)


def _normalize_utc_timestamp(value: datetime | None) -> datetime | None:
    """Normalize persisted or incoming datetimes before recovery comparisons."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _recovery_timestamp(value: datetime | None) -> str | None:
    aware = _normalize_utc_timestamp(value)
    return aware.isoformat() if aware is not None else None


def _recovery_json_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


_MEMORY_EXPORT_ENVELOPE_FIELDS = (
    "schema_version",
    "owner_session_id",
    "canonical_tombstone_revision",
    "memories",
    "tombstones",
    "m5_proposals",
    "m5_decision_receipts",
    "generated_at",
    "status",
    "operator_status",
    "provenance",
    "no_learning_reason",
    "reconciliation",
    "counts",
    "memory_ids",
    "tombstone_ids",
)

# Recovery archives are operator-authenticated snapshots.  These validators
# keep M5 rows as identifiers/digests/enums only; proposal text, scope,
# provenance, and source bodies never cross the archive boundary.
_M5_RECOVERY_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")
_M5_RECOVERY_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_M5_SCOPE_SCHEMA_VERSION = "memory_scope.v1"
_M5_SCOPE_KEYS = frozenset(
    {
        "schema_version",
        "goal_id",
        "goal_revision",
        "source_context_digest",
        "preferred_capability_id",
        "preferred_capability_version",
        "candidate_capability_ids",
    }
)
_M5_SOURCE_EVIDENCE_LIMIT = 20

_M5_ROLLBACK_BINDING_SCHEMA_VERSION = "work-board-m5-rollback-binding.v2"
_M5_SELECTION_BINDING_SCHEMA_VERSION = "work-board-m5-selection-binding.v3"


def _m5_rollback_binding_key_id() -> str:
    """Return a non-secret identifier for the active rollback signing key."""

    return hashlib.sha256(
        b"seraph-m5-rollback-key-id-v1:" + _effect_mac_key()
    ).hexdigest()[:24]


def _m5_rollback_binding_mac(
    *,
    memory_id: str,
    proposal_id: str,
    accepted_content_digest: str,
    owner_principal_id: str,
    owner_session_id: str,
    rollback_at: datetime | str,
    rollback_reason: str,
) -> str:
    """Authenticate one operator rollback marker with existing server key material."""

    if isinstance(rollback_at, datetime):
        normalized_rollback_at = _recovery_timestamp(_normalize_utc_timestamp(rollback_at))
    else:
        parsed_rollback_at = _parse_recovery_timestamp(
            rollback_at,
            field_name="rollback_at",
        )
        normalized_rollback_at = _recovery_timestamp(parsed_rollback_at)
    values = {
        "memory_id": str(memory_id or "").strip(),
        "proposal_id": str(proposal_id or "").strip(),
        "accepted_content_digest": str(accepted_content_digest or "").strip().lower(),
        "owner_principal_id": str(owner_principal_id or "").strip(),
        "owner_session_id": str(owner_session_id or "").strip(),
        "rollback_at": normalized_rollback_at,
        "rollback_reason": str(rollback_reason or "")[:500],
        "key_id": _m5_rollback_binding_key_id(),
    }
    if (
        not values["memory_id"]
        or not values["proposal_id"]
        or not _M5_RECOVERY_DIGEST.fullmatch(values["accepted_content_digest"])
        or not values["owner_principal_id"]
        or not values["owner_session_id"]
        or not values["rollback_at"]
    ):
        raise ValueError("M5 rollback binding identity is invalid")
    return _mac(
        {
            "version": _M5_ROLLBACK_BINDING_SCHEMA_VERSION,
            "rollback": values,
        },
        key=_effect_mac_key(),
    )
def _m5_selection_scope(value: Any) -> dict[str, Any] | None:
    """Project the selection fields bound into an accepted M5 memory.

    ``m5_memory_scope`` also contains owner and source execution fields.  The
    recovery archive intentionally keeps only the bounded scope fields, so the
    comparison must use the same projection on both sides.  A missing or
    malformed canonical projection fails closed.
    """

    if not isinstance(value, dict) or value.get("schema_version") != _M5_SCOPE_SCHEMA_VERSION:
        return None
    required = {
        "goal_id",
        "goal_revision",
        "source_context_digest",
        "preferred_capability_id",
        "candidate_capability_ids",
    }
    if not required.issubset(value):
        return None
    candidate_ids = value.get("candidate_capability_ids")
    if not isinstance(candidate_ids, list) or any(not isinstance(item, str) for item in candidate_ids):
        return None
    return {
        "schema_version": value["schema_version"],
        "goal_id": value["goal_id"],
        "goal_revision": value["goal_revision"],
        "source_context_digest": value["source_context_digest"],
        "preferred_capability_id": value.get("preferred_capability_id"),
        "preferred_capability_version": value.get("preferred_capability_version"),
        "candidate_capability_ids": list(candidate_ids),
    }


def _m5_correction_binding(value: Any) -> dict[str, str | None] | None:
    """Return the bounded supersession target covered by an M5 signature.

    A correction changes the rollback boundary: undoing the new memory may
    reinstate the target's previous status.  Keep both values in the keyed
    binding so a rehashed archive cannot redirect or alter that boundary.
    """

    if isinstance(value, dict):
        source = value
    else:
        source = value

    def _field(name: str) -> Any:
        if isinstance(source, dict):
            return source.get(name)
        return getattr(source, name, None)

    target_id = str(_field("corrects_memory_id") or "").strip() or None
    previous_status = str(_field("corrected_memory_previous_status") or "").strip() or None
    raw_target_digest = str(_field("corrected_memory_content_digest") or "").strip().lower()
    target_digest = raw_target_digest or None
    if bool(target_id) != bool(previous_status) or bool(target_id) != bool(target_digest):
        return None
    if target_id and not _M5_RECOVERY_ID.fullmatch(target_id):
        return None
    if target_digest and not _M5_RECOVERY_DIGEST.fullmatch(target_digest):
        return None
    if previous_status and previous_status not in {"active", "superseded", "archived"}:
        return None
    return {
        "corrects_memory_id": target_id,
        "corrected_memory_previous_status": previous_status,
        "corrected_memory_content_digest": target_digest,
    }


def _m5_verified_source_binding(value: Any) -> dict[str, Any] | None:
    """Return the bounded immutable source fields used by M5 decisions."""

    if isinstance(value, dict):
        raw = value.get("verified_source_binding", value)

        def get_field(name: str, default: Any = None) -> Any:
            return raw.get(name, default) if isinstance(raw, dict) else default

    else:

        def get_field(name: str, default: Any = None) -> Any:
            return getattr(value, name, default)

    if isinstance(value, dict) and isinstance(raw, dict) and "source_evidence_ids" in raw:
        evidence_value = raw.get("source_evidence_ids")
    else:
        evidence_value = get_field("source_evidence_ids")
        if evidence_value is None:
            evidence_value = get_field("source_refs_json", "[]")
    if isinstance(evidence_value, str):
        try:
            evidence_value = json.loads(evidence_value)
        except (TypeError, ValueError):
            return None
    if not isinstance(evidence_value, list) or any(not isinstance(item, str) for item in evidence_value):
        return None

    try:
        normalized = {
            "owner_principal_id": str(get_field("owner_principal_id") or ""),
            "owner_session_id": str(get_field("owner_session_id") or ""),
            "source_task_id": str(get_field("source_task_id") or ""),
            "source_task_revision": int(get_field("source_task_revision") or 0),
            "source_attempt_id": str(get_field("source_attempt_id") or ""),
            "source_attempt_fence": int(get_field("source_attempt_fence") or 0),
            "workflow_run_id": str(get_field("workflow_run_id") or ""),
            "workflow_run_revision": int(get_field("workflow_run_revision") or 0),
            "goal_id": str(get_field("goal_id") or ""),
            "goal_revision": int(get_field("goal_revision") or 0),
            "capability_id": str(get_field("capability_id") or ""),
            "capability_version": str(get_field("capability_version") or ""),
            "typed_input_digest": str(get_field("typed_input_digest") or ""),
            "source_context_digest": str(get_field("source_context_digest") or ""),
            "candidate_set_digest": str(get_field("candidate_set_digest") or ""),
            "evidence_digest": str(get_field("evidence_digest") or ""),
            "readback_kind": str(get_field("readback_kind") or ""),
            "readback_ref": str(get_field("readback_ref") or ""),
            "readback_digest": str(get_field("readback_digest") or ""),
            "artifact_ref": str(get_field("artifact_ref") or ""),
            "artifact_digest": str(get_field("artifact_digest") or ""),
            "proposal_job_id": str(get_field("proposal_job_id") or ""),
            "request_idempotency_key": str(get_field("request_idempotency_key") or ""),
            "request_binding_digest": str(get_field("request_binding_digest") or ""),
            "source_evidence_ids": list(evidence_value),
        }
    except (TypeError, ValueError, OverflowError):
        return None
    if not normalized["owner_principal_id"] or not normalized["owner_session_id"]:
        return None
    if not normalized["source_task_id"] or not normalized["source_attempt_id"]:
        return None
    if not normalized["goal_id"] or not normalized["capability_id"]:
        return None
    return normalized


def _m5_selection_binding_key_id() -> str:
    """Return a non-secret identifier for the active M5 signing key."""

    return hashlib.sha256(b"seraph-m5-selection-key-id-v1:" + _effect_mac_key()).hexdigest()[:24]


def _m5_selection_binding_mac(
    *,
    proposal_id: str,
    accepted_content_digest: str,
    owner_principal_id: str,
    owner_session_id: str,
    source_context_digest: str,
    source_binding: Any,
    decision_effect: Any,
    memory_scope: Any,
    corrects_memory_id: str | None = None,
    corrected_memory_previous_status: str | None = None,
    corrected_memory_content_digest: str | None = None,
    recovered_from_proposal_id: str | None = None,
    lifecycle_state: str = "active",
    lifecycle_at: str | None = None,
    lifecycle_reason: str | None = None,
) -> str:
    """Authenticate the accepted M5 selection with existing server key material."""

    canonical_scope = _m5_selection_scope(memory_scope)
    if canonical_scope is None:
        raise ValueError("M5 selection scope is invalid")
    if (
        not owner_principal_id
        or not owner_session_id
        or not source_context_digest
        or source_context_digest != canonical_scope["source_context_digest"]
    ):
        raise ValueError("M5 selection binding identity is invalid")
    canonical_source = _m5_verified_source_binding(source_binding)
    if canonical_source is None:
        raise ValueError("M5 verified source binding is invalid")
    if (
        canonical_source["owner_principal_id"] != owner_principal_id
        or canonical_source["owner_session_id"] != owner_session_id
        or canonical_source["source_context_digest"] != source_context_digest
    ):
        raise ValueError("M5 verified source identity is invalid")
    canonical_effect = str(getattr(decision_effect, "value", decision_effect) or "").strip()
    if not canonical_effect:
        raise ValueError("M5 decision effect is invalid")
    canonical_correction = _m5_correction_binding(
        {
            "corrects_memory_id": corrects_memory_id,
            "corrected_memory_previous_status": corrected_memory_previous_status,
            "corrected_memory_content_digest": corrected_memory_content_digest,
        }
    )
    if canonical_correction is None:
        raise ValueError("M5 correction target binding is invalid")
    canonical_recovery_parent = str(recovered_from_proposal_id or "").strip() or None
    if canonical_recovery_parent and not _M5_RECOVERY_ID.fullmatch(canonical_recovery_parent):
        raise ValueError("M5 recovery parent binding is invalid")
    canonical_lifecycle_state = str(lifecycle_state or "").strip()
    if canonical_lifecycle_state not in {"active", "rolled_back"}:
        raise ValueError("M5 selection lifecycle state is invalid")
    payload = {
        "version": _M5_SELECTION_BINDING_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "accepted_content_digest": accepted_content_digest,
        "owner_principal_id": owner_principal_id,
        "owner_session_id": owner_session_id,
        "source_context_digest": source_context_digest,
        "verified_source_binding": canonical_source,
        "decision_effect": canonical_effect,
        "memory_scope": canonical_scope,
        "correction_target": canonical_correction,
        "recovered_from_proposal_id": canonical_recovery_parent,
        "lifecycle_state": canonical_lifecycle_state,
    }
    if canonical_lifecycle_state == "rolled_back":
        normalized_lifecycle_at = str(lifecycle_at or "").strip()
        normalized_lifecycle_reason = str(lifecycle_reason or "").strip()
        if not normalized_lifecycle_at or not normalized_lifecycle_reason or len(normalized_lifecycle_reason) > 500:
            raise ValueError("M5 rollback lifecycle metadata is invalid")
        payload["lifecycle_at"] = normalized_lifecycle_at
        payload["lifecycle_reason"] = normalized_lifecycle_reason
    return _mac(payload, key=_effect_mac_key())


def _m5_selection_binding_matches(
    provenance: Any,
    *,
    proposal_id: Any,
    accepted_content_digest: Any,
    decision_effect: Any,
    memory_scope: Any,
    source_binding: Any,
    corrects_memory_id: str | None = None,
    recovered_from_proposal_id: str | None = None,
) -> bool:
    """Confirm that an accepted proposal still matches canonical memory.

    Recovery archive hashes are public integrity checks, not an operator
    signature.  The canonical Memory provenance therefore remains the binding
    authority for fields that can change a later task decision.
    """

    if not isinstance(provenance, dict):
        return False
    if provenance.get("proposal_id") != proposal_id:
        return False
    if provenance.get("accepted_content_digest") != accepted_content_digest:
        return False
    lifecycle_state = provenance.get("lifecycle_state")
    if lifecycle_state != "active":
        return False
    expected_effect = str(getattr(decision_effect, "value", decision_effect) or "").strip()
    canonical_effect = str(provenance.get("decision_effect") or "").strip()
    if not expected_effect or canonical_effect != expected_effect:
        return False
    expected_scope = _m5_selection_scope(memory_scope)
    canonical_scope = _m5_selection_scope(provenance.get("memory_scope"))
    if expected_scope is None or canonical_scope != expected_scope:
        return False
    expected_source = _m5_verified_source_binding(source_binding)
    canonical_source = _m5_verified_source_binding(provenance.get("verified_source_binding"))
    if expected_source is None or canonical_source != expected_source:
        return False
    expected_recovery_parent = (
        str(recovered_from_proposal_id).strip()
        if recovered_from_proposal_id is not None
        else str(getattr(source_binding, "recovered_from_proposal_id", "") or "").strip()
        if not isinstance(source_binding, dict)
        else str(source_binding.get("recovered_from_proposal_id") or "").strip()
    ) or None
    canonical_recovery_parent = str(provenance.get("recovered_from_proposal_id") or "").strip() or None
    if canonical_recovery_parent != expected_recovery_parent:
        return False
    expected_correction_id = (
        str(corrects_memory_id).strip()
        if corrects_memory_id is not None
        else str(getattr(source_binding, "corrects_memory_id", "") or "").strip()
        if not isinstance(source_binding, dict)
        else str(source_binding.get("corrects_memory_id") or "").strip()
    ) or None
    canonical_correction = _m5_correction_binding(provenance)
    if canonical_correction is None or canonical_correction["corrects_memory_id"] != expected_correction_id:
        return False
    if expected_correction_id and canonical_correction["corrected_memory_previous_status"] != "active":
        return False
    try:
        active_key_id = _m5_selection_binding_key_id()
        stored_key_id = provenance.get("selection_binding_key_id")
        if not hmac.compare_digest(
            stored_key_id if isinstance(stored_key_id, str) else "", active_key_id
        ):
            return False
        expected_mac = _m5_selection_binding_mac(
            proposal_id=proposal_id,
            accepted_content_digest=accepted_content_digest,
            owner_principal_id=str(provenance.get("owner_principal_id") or ""),
            owner_session_id=str(provenance.get("owner_session_id") or ""),
            source_context_digest=str(provenance.get("source_context_digest") or ""),
            source_binding=canonical_source,
            decision_effect=canonical_effect,
            memory_scope=canonical_scope,
            corrects_memory_id=canonical_correction["corrects_memory_id"],
            corrected_memory_previous_status=canonical_correction["corrected_memory_previous_status"],
            corrected_memory_content_digest=canonical_correction[
                "corrected_memory_content_digest"
            ],
            recovered_from_proposal_id=canonical_recovery_parent,
            lifecycle_state="active",
        )
    except (CapabilityJournalError, TypeError, ValueError):
        return False
    stored_mac = provenance.get("selection_binding_mac")
    return isinstance(stored_mac, str) and hmac.compare_digest(stored_mac, expected_mac)


def _m5_selection_binding_failure_reason(
    provenance: Any,
    *,
    proposal_id: Any,
    accepted_content_digest: Any,
    decision_effect: Any,
    memory_scope: Any,
    source_binding: Any,
    corrects_memory_id: str | None = None,
    recovered_from_proposal_id: str | None = None,
) -> str:
    """Classify a rejected accepted-memory binding without exposing key state."""

    if not isinstance(provenance, dict):
        return "unverifiable"
    if not provenance.get("selection_binding_mac") or not provenance.get("verified_source_binding"):
        return "unverifiable"
    if provenance.get("lifecycle_state") != "active":
        return "mismatch" if provenance.get("lifecycle_state") == "rolled_back" else "unverifiable"
    try:
        active_key_id = _m5_selection_binding_key_id()
    except CapabilityJournalError:
        return "unverifiable"
    stored_key_id = provenance.get("selection_binding_key_id")
    if (
        not isinstance(stored_key_id, str)
        or not re.fullmatch(r"[0-9a-f]{24}", stored_key_id)
        or not hmac.compare_digest(stored_key_id, active_key_id)
    ):
        return "unverifiable"
    expected_source = _m5_verified_source_binding(source_binding)
    canonical_source = _m5_verified_source_binding(provenance.get("verified_source_binding"))
    expected_scope = _m5_selection_scope(memory_scope)
    canonical_scope = _m5_selection_scope(provenance.get("memory_scope"))
    if expected_source is None or canonical_source is None or expected_scope is None:
        return "unverifiable"
    if canonical_source != expected_source or canonical_scope != expected_scope:
        return "mismatch"
    expected_recovery_parent = (
        str(recovered_from_proposal_id).strip()
        if recovered_from_proposal_id is not None
        else str(getattr(source_binding, "recovered_from_proposal_id", "") or "").strip()
        if not isinstance(source_binding, dict)
        else str(source_binding.get("recovered_from_proposal_id") or "").strip()
    ) or None
    canonical_recovery_parent = str(provenance.get("recovered_from_proposal_id") or "").strip() or None
    if canonical_recovery_parent != expected_recovery_parent:
        return "mismatch"
    expected_correction_id = (
        str(corrects_memory_id).strip()
        if corrects_memory_id is not None
        else str(getattr(source_binding, "corrects_memory_id", "") or "").strip()
        if not isinstance(source_binding, dict)
        else str(source_binding.get("corrects_memory_id") or "").strip()
    ) or None
    canonical_correction = _m5_correction_binding(provenance)
    if canonical_correction is None:
        return "unverifiable"
    if canonical_correction["corrects_memory_id"] != expected_correction_id:
        return "mismatch"
    if expected_correction_id and canonical_correction["corrected_memory_previous_status"] != "active":
        return "mismatch"
    try:
        expected_mac = _m5_selection_binding_mac(
            proposal_id=proposal_id,
            accepted_content_digest=accepted_content_digest,
            owner_principal_id=str(provenance.get("owner_principal_id") or ""),
            owner_session_id=str(provenance.get("owner_session_id") or ""),
            source_context_digest=str(provenance.get("source_context_digest") or ""),
            source_binding=canonical_source,
            decision_effect=decision_effect,
            memory_scope=canonical_scope,
            corrects_memory_id=canonical_correction["corrects_memory_id"],
            corrected_memory_previous_status=canonical_correction["corrected_memory_previous_status"],
            corrected_memory_content_digest=canonical_correction[
                "corrected_memory_content_digest"
            ],
            recovered_from_proposal_id=canonical_recovery_parent,
            lifecycle_state="active",
        )
    except CapabilityJournalError:
        return "unverifiable"
    except (TypeError, ValueError):
        return "mismatch"
    stored_mac = provenance.get("selection_binding_mac")
    if (
        not isinstance(stored_mac, str)
        or not re.fullmatch(r"[0-9a-f]{64}", stored_mac)
        or not hmac.compare_digest(stored_mac, expected_mac)
    ):
        return "mismatch"
    return "mismatch"


def _m5_rollback_lifecycle_state(
    provenance: Any,
    *,
    proposal_id: str,
    accepted_content_digest: str,
    owner_principal_id: str,
    owner_session_id: str,
) -> str:
    """Verify rollback state retained in the accepted-memory signature.

    The separate rollback marker carries a user-facing reason and timestamp,
    but an archive may omit that marker and recompute its public hashes. The
    selection signature is therefore re-signed at rollback with the terminal
    lifecycle, timestamp, and reason so removing the marker cannot reactivate
    the former acceptance.
    """

    if not isinstance(provenance, dict):
        return "absent"
    lifecycle_state = provenance.get("lifecycle_state")
    if lifecycle_state in {None, "active"}:
        return "absent"
    if lifecycle_state != "rolled_back":
        return "unverifiable"
    try:
        active_key_id = _m5_selection_binding_key_id()
        lifecycle_at = str(provenance.get("lifecycle_at") or "").strip()
        lifecycle_reason = str(provenance.get("lifecycle_reason") or "").strip()
        stored_key_id = provenance.get("selection_binding_key_id")
        stored_mac = provenance.get("selection_binding_mac")
        if (
            not isinstance(stored_key_id, str)
            or not re.fullmatch(r"[0-9a-f]{24}", stored_key_id)
            or not hmac.compare_digest(stored_key_id, active_key_id)
            or not isinstance(stored_mac, str)
            or not re.fullmatch(r"[0-9a-f]{64}", stored_mac)
            or not lifecycle_at
            or not lifecycle_reason
            or len(lifecycle_reason) > 500
        ):
            return "unverifiable"
        normalized_at = _recovery_timestamp(
            _parse_recovery_timestamp(lifecycle_at, field_name="M5 rollback lifecycle timestamp")
        )
        if normalized_at is None or normalized_at != lifecycle_at:
            return "unverifiable"
        if (
            provenance.get("proposal_id") != proposal_id
            or provenance.get("accepted_content_digest") != accepted_content_digest
            or provenance.get("owner_principal_id") != owner_principal_id
            or provenance.get("owner_session_id") != owner_session_id
        ):
            return "mismatch"
        expected_mac = _m5_selection_binding_mac(
            proposal_id=proposal_id,
            accepted_content_digest=accepted_content_digest,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            source_context_digest=str(provenance.get("source_context_digest") or ""),
            source_binding=provenance.get("verified_source_binding"),
            decision_effect=provenance.get("decision_effect"),
            memory_scope=provenance.get("memory_scope"),
            corrects_memory_id=_m5_correction_binding(provenance)["corrects_memory_id"]
            if _m5_correction_binding(provenance) is not None
            else None,
            corrected_memory_previous_status=_m5_correction_binding(provenance)[
                "corrected_memory_previous_status"
            ]
            if _m5_correction_binding(provenance) is not None
            else None,
            corrected_memory_content_digest=_m5_correction_binding(provenance)[
                "corrected_memory_content_digest"
            ]
            if _m5_correction_binding(provenance) is not None
            else None,
            recovered_from_proposal_id=provenance.get("recovered_from_proposal_id"),
            lifecycle_state="rolled_back",
            lifecycle_at=lifecycle_at,
            lifecycle_reason=lifecycle_reason,
        )
    except CapabilityJournalError:
        return "unverifiable"
    except (TypeError, ValueError):
        return "mismatch"
    return "valid" if hmac.compare_digest(stored_mac, expected_mac) else "mismatch"


def _m5_receipt_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


_M5_RECEIPT_INTEGRITY_FIELDS = (
    "receipt_id",
    "schema_version",
    "receipt_stage",
    "receipt_binding_digest",
    "owner_principal_id",
    "owner_session_id",
    "source_proposal_id",
    "source_proposal_revision",
    "source_baseline_receipt_id",
    "source_task_id",
    "source_task_revision",
    "source_attempt_id",
    "source_attempt_fence",
    "source_workflow_run_id",
    "source_workflow_run_revision",
    "later_task_id",
    "later_task_revision",
    "later_attempt_id",
    "later_workflow_run_id",
    "later_attempt_fence",
    "goal_id",
    "goal_revision",
    "capability_id",
    "capability_version",
    "typed_input_digest",
    "task_intent_digest",
    "source_context_digest",
    "candidate_set_digest",
    "accepted_memory_id",
    "accepted_memory_content_digest",
    "before_input_digest",
    "after_input_digest",
    "before_action_id",
    "after_action_id",
    "before_selected_capability_id",
    "after_selected_capability_id",
    "confirmed_action_id",
    "comparison_context_digest",
    "retrieval_evidence_ids",
    "decision_status",
    "admission_status",
    "reason",
    "confirmer_principal_id",
    "confirmer_session_id",
    "confirmed_at",
    "confirmation_binding_digest",
    "consumed_at",
    "revision",
    "created_at",
    "updated_at",
)


def _m5_receipt_integrity_payload(value: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name in _M5_RECEIPT_INTEGRITY_FIELDS:
        if field_name == "retrieval_evidence_ids":
            raw_evidence = _m5_receipt_field(value, field_name)
            if raw_evidence is None:
                raw_evidence = _m5_receipt_field(value, "retrieval_evidence_ids_json", "[]")
            if isinstance(raw_evidence, str):
                try:
                    raw_evidence = json.loads(raw_evidence)
                except (TypeError, ValueError):
                    raw_evidence = []
            payload[field_name] = raw_evidence if isinstance(raw_evidence, list) else []
            continue
        raw = _m5_receipt_field(value, field_name)
        if field_name in {"receipt_stage", "decision_status", "admission_status"}:
            raw = getattr(raw, "value", raw)
        elif field_name in {"confirmed_at", "consumed_at", "created_at", "updated_at"}:
            if raw is not None:
                if isinstance(raw, datetime):
                    raw = _recovery_timestamp(raw)
                elif isinstance(raw, str):
                    try:
                        raw = _recovery_timestamp(
                            _parse_recovery_timestamp(raw, field_name=f"receipt.{field_name}")
                        )
                    except ValueError:
                        # Normalized restore records have already validated
                        # timestamps.  Keep malformed values deterministic for
                        # the comparison helper, which will fail closed.
                        raw = str(raw)
                else:
                    raw = str(raw)
        payload[field_name] = raw
    return payload


def _m5_receipt_integrity_mac(value: Any) -> str:
    return _mac(
        {
            "version": "work-board-m5-receipt-integrity.v1",
            "receipt": _m5_receipt_integrity_payload(value),
        },
        key=_effect_mac_key(),
    )


def _m5_receipt_integrity_matches(value: Any) -> bool:
    """Verify the complete receipt record with the configured server key."""

    supplied = _m5_receipt_field(value, "receipt_integrity_mac")
    if not isinstance(supplied, str) or not _M5_RECOVERY_DIGEST.fullmatch(supplied):
        return False
    try:
        expected = _m5_receipt_integrity_mac(value)
    except (CapabilityJournalError, TypeError, ValueError):
        return False
    return hmac.compare_digest(supplied, expected)


def _m5_receipt_binding_digest(receipt: Any, proposal: Any | None = None) -> str | None:
    """Recompute one historical M5 receipt binding from canonical fields."""

    from src.memory.m5 import M5_NONE_DIGEST, M5_NONE_MEMORY, M5_NONE_PROPOSAL, M5_RECEIPT_SCHEMA_VERSION, m5_digest

    stage = str(getattr(_m5_receipt_field(receipt, "receipt_stage"), "value", _m5_receipt_field(receipt, "receipt_stage") or ""))
    owner_principal_id = str(_m5_receipt_field(receipt, "owner_principal_id") or "")
    owner_session_id = str(_m5_receipt_field(receipt, "owner_session_id") or "")
    if stage == "later_comparison":
        return m5_digest(
            {
                "version": M5_RECEIPT_SCHEMA_VERSION,
                "stage": stage,
                "owner_principal_id": owner_principal_id,
                "owner_session_id": owner_session_id,
                "later_task_id": _m5_receipt_field(receipt, "later_task_id"),
                "later_task_revision": int(_m5_receipt_field(receipt, "later_task_revision") or 0),
                "goal_id": _m5_receipt_field(receipt, "goal_id"),
                "goal_revision": int(_m5_receipt_field(receipt, "goal_revision") or 0),
                "source_context_digest": str(_m5_receipt_field(receipt, "source_context_digest") or "").lower(),
                "candidate_set_digest": _m5_receipt_field(receipt, "candidate_set_digest") or "",
                "accepted_proposal_id": _m5_receipt_field(receipt, "source_proposal_id") or M5_NONE_PROPOSAL,
                "source_proposal_revision": int(_m5_receipt_field(receipt, "source_proposal_revision") or 0),
                "accepted_memory_id": _m5_receipt_field(receipt, "accepted_memory_id") or M5_NONE_MEMORY,
                "accepted_memory_content_digest": _m5_receipt_field(receipt, "accepted_memory_content_digest") or M5_NONE_DIGEST,
            }
        )
    if stage != "source_baseline":
        return None
    if str(_m5_receipt_field(receipt, "reason") or "") == "source_baseline":
        return m5_digest(
            {
                "version": M5_RECEIPT_SCHEMA_VERSION,
                "stage": stage,
                "owner_principal_id": owner_principal_id,
                "owner_session_id": owner_session_id,
                "source_attempt_id": _m5_receipt_field(receipt, "source_attempt_id"),
                "source_task_revision": int(_m5_receipt_field(receipt, "source_task_revision") or 0),
                "source_proposal_id": _m5_receipt_field(receipt, "source_proposal_id"),
                "source_proposal_revision": int(_m5_receipt_field(receipt, "source_proposal_revision") or 0),
            }
        )
    if proposal is None:
        return None
    status = str(getattr(_m5_receipt_field(proposal, "status"), "value", _m5_receipt_field(proposal, "status") or ""))
    return m5_digest(
        {
            "version": M5_RECEIPT_SCHEMA_VERSION,
            "stage": stage,
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
            "source_task_id": _m5_receipt_field(receipt, "source_task_id"),
            "source_task_revision": int(_m5_receipt_field(receipt, "source_task_revision") or 0),
            "source_attempt_id": _m5_receipt_field(receipt, "source_attempt_id"),
            "source_proposal_id": _m5_receipt_field(receipt, "source_proposal_id"),
            "source_proposal_revision": int(_m5_receipt_field(receipt, "source_proposal_revision") or 0),
            "status": status,
            "reason": _m5_receipt_field(receipt, "reason") or "",
        }
    )


def _m5_receipt_source_matches_proposal(receipt: Any, proposal: Any) -> bool:
    source = _m5_verified_source_binding(proposal)
    if source is None:
        return False
    if _m5_receipt_field(receipt, "source_proposal_id") != _m5_receipt_field(proposal, "proposal_id"):
        return False
    try:
        source_proposal_revision = int(_m5_receipt_field(receipt, "source_proposal_revision") or 0)
        current_proposal_revision = int(_m5_receipt_field(proposal, "revision") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if source_proposal_revision < 1 or source_proposal_revision > current_proposal_revision:
        return False
    for receipt_name, source_name in (
        ("owner_principal_id", "owner_principal_id"),
        ("owner_session_id", "owner_session_id"),
        ("source_task_id", "source_task_id"),
        ("source_task_revision", "source_task_revision"),
        ("source_attempt_id", "source_attempt_id"),
        ("source_attempt_fence", "source_attempt_fence"),
        ("source_workflow_run_id", "workflow_run_id"),
        ("source_workflow_run_revision", "workflow_run_revision"),
        ("goal_id", "goal_id"),
        ("goal_revision", "goal_revision"),
        ("source_context_digest", "source_context_digest"),
    ):
        receipt_value = _m5_receipt_field(receipt, receipt_name)
        if receipt_name == "source_workflow_run_id" and receipt_value is None:
            receipt_value = ""
        if str(receipt_value or "") != str(source.get(source_name) or ""):
            return False
    try:
        receipt_evidence = json.loads(_m5_receipt_field(receipt, "retrieval_evidence_ids_json") or "[]")
    except (TypeError, ValueError):
        return False
    return receipt_evidence == source["source_evidence_ids"]


def _m5_receipt_binding_matches(receipt: Any, proposal: Any | None = None) -> bool:
    if proposal is not None and not _m5_receipt_source_matches_proposal(receipt, proposal):
        return False
    expected = _m5_receipt_binding_digest(receipt, proposal)
    supplied = _m5_receipt_field(receipt, "receipt_binding_digest")
    return bool(
        expected
        and isinstance(supplied, str)
        and _M5_RECOVERY_DIGEST.fullmatch(supplied)
        and hmac.compare_digest(supplied, expected)
    )


def _m5_recovery_id(value: Any, *, field_name: str, required: bool = False) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        if required:
            raise ValueError(f"memory restore M5 {field_name} is required")
        return None
    if not _M5_RECOVERY_ID.fullmatch(candidate):
        raise ValueError(f"memory restore M5 {field_name} is not a safe identifier")
    return candidate


def _m5_recovery_text(value: Any, *, field_name: str, maximum: int = 1000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise ValueError(f"memory restore M5 {field_name} is invalid")
    return value


def _m5_recovery_digest(
    value: Any,
    *,
    field_name: str,
    required: bool = False,
) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"memory restore M5 {field_name} is required")
        return None
    candidate = str(value).strip().lower()
    if not _M5_RECOVERY_DIGEST.fullmatch(candidate):
        raise ValueError(f"memory restore M5 {field_name} must be a SHA-256 digest")
    return candidate


def _m5_recovery_int(
    value: Any,
    *,
    field_name: str,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"memory restore M5 {field_name} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"memory restore M5 {field_name} is invalid") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"memory restore M5 {field_name} is out of bounds")
    return parsed


def _m5_recovery_enum(value: Any, enum_cls: type, *, field_name: str) -> Any:
    try:
        return value if isinstance(value, enum_cls) else enum_cls(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"memory restore M5 {field_name} is invalid") from exc


def _m5_recovery_timestamp(value: Any, *, field_name: str, required: bool = False) -> datetime | None:
    parsed = _parse_recovery_timestamp(value, field_name=field_name)
    if required and parsed is None:
        raise ValueError(f"memory restore M5 {field_name} is required")
    return parsed


def _m5_recovery_evidence_ids(
    value: Any,
    *,
    field_name: str = "receipt evidence ids",
    maximum: int = _MAX_RECOVERY_SOURCES_PER_RECORD,
) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"memory restore M5 {field_name} are invalid") from exc
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"memory restore M5 {field_name} are invalid")
    normalized: list[str] = []
    for index, item in enumerate(value):
        normalized_id = _m5_recovery_id(
            item,
            field_name=f"{field_name[:-1] if field_name.endswith('s') else field_name} {index}",
            required=True,
        )
        if normalized_id is None:  # pragma: no cover - required=True raises first
            raise ValueError(f"memory restore M5 {field_name} are invalid")
        normalized.append(normalized_id)
    return json.dumps(normalized, separators=(",", ":"))


def _m5_export_enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _m5_export_evidence_ids(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        item.strip()
        for item in parsed
        if isinstance(item, str) and _M5_RECOVERY_ID.fullmatch(item.strip())
    ]


def _m5_export_source_evidence_ids(value: Any) -> list[str]:
    return _m5_export_evidence_ids(value)[:_M5_SOURCE_EVIDENCE_LIMIT]


def _m5_recovery_scope(
    value: Any,
    *,
    require_preferred_version: bool = False,
    reject_unknown_keys: bool = True,
) -> tuple[str | None, bool]:
    """Keep only the bounded scope needed for later M5 candidate matching.

    Old archives do not contain this object, and malformed or over-broad scope
    is treated as unavailable.  The caller can then restore the proposal as a
    visible blocked record with recovery guidance instead of granting an
    implicit decision source.
    """

    if value is None or value == "":
        return None, False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None, False
    if not isinstance(value, dict) or (
        reject_unknown_keys and not set(value).issubset(_M5_SCOPE_KEYS)
    ):
        return None, False
    try:
        if value.get("schema_version") != _M5_SCOPE_SCHEMA_VERSION:
            return None, False
        goal_id = _m5_recovery_id(value.get("goal_id"), field_name="scope goal_id", required=True)
        goal_revision = _m5_recovery_int(
            value.get("goal_revision"), field_name="scope goal_revision", minimum=1
        )
        source_context_digest = _m5_recovery_digest(
            value.get("source_context_digest"),
            field_name="scope source_context_digest",
            required=True,
        )
        preferred_capability_id = _m5_recovery_id(
            value.get("preferred_capability_id"), field_name="scope preferred_capability_id"
        )
        preferred_capability_version = _m5_recovery_id(
            value.get("preferred_capability_version"),
            field_name="scope preferred_capability_version",
        )
        if preferred_capability_version and not preferred_capability_id:
            return None, False
        if require_preferred_version and preferred_capability_id and not preferred_capability_version:
            return None, False
        candidate_values = value.get("candidate_capability_ids")
        if not isinstance(candidate_values, list) or len(candidate_values) > _M5_SOURCE_EVIDENCE_LIMIT:
            return None, False
        candidate_capability_ids: list[str] = []
        for index, candidate in enumerate(candidate_values):
            normalized_candidate = _m5_recovery_id(
                candidate,
                field_name=f"scope candidate_capability_id {index}",
                required=True,
            )
            if normalized_candidate is not None and normalized_candidate not in candidate_capability_ids:
                candidate_capability_ids.append(normalized_candidate)
        if preferred_capability_id and preferred_capability_id not in candidate_capability_ids:
            return None, False
    except (TypeError, ValueError, OverflowError):
        return None, False
    normalized = {
        "schema_version": _M5_SCOPE_SCHEMA_VERSION,
        "goal_id": goal_id,
        "goal_revision": goal_revision,
        "source_context_digest": source_context_digest,
        "preferred_capability_id": preferred_capability_id,
        "preferred_capability_version": preferred_capability_version,
        "candidate_capability_ids": candidate_capability_ids,
    }
    return json.dumps(normalized, sort_keys=True, separators=(",", ":")), True


def _m5_proposal_archive_payload(proposal: MemoryProposal) -> dict[str, Any]:
    """Serialize the non-content fields needed to recover one M5 proposal."""

    scope_json, _scope_valid = _m5_recovery_scope(
        proposal.memory_scope_json,
        reject_unknown_keys=False,
    )
    scope = json.loads(scope_json) if scope_json else None

    return {
        "proposal_id": proposal.proposal_id,
        "recovered_from_proposal_id": proposal.recovered_from_proposal_id,
        "schema_version": proposal.schema_version,
        "owner_principal_id": proposal.owner_principal_id,
        "owner_session_id": proposal.owner_session_id,
        "source_task_id": proposal.source_task_id,
        "source_task_revision": proposal.source_task_revision,
        "source_attempt_id": proposal.source_attempt_id,
        "source_attempt_fence": proposal.source_attempt_fence,
        "workflow_run_id": proposal.workflow_run_id,
        "workflow_run_revision": proposal.workflow_run_revision,
        "goal_id": proposal.goal_id,
        "goal_revision": proposal.goal_revision,
        "capability_id": proposal.capability_id,
        "capability_version": proposal.capability_version,
        "typed_input_digest": proposal.typed_input_digest,
        "source_context_digest": proposal.source_context_digest,
        "candidate_set_digest": proposal.candidate_set_digest,
        "evidence_digest": proposal.evidence_digest,
        "readback_kind": proposal.readback_kind,
        "readback_ref": proposal.readback_ref,
        "readback_digest": proposal.readback_digest,
        "artifact_ref": proposal.artifact_ref,
        "artifact_digest": proposal.artifact_digest,
        "proposal_job_id": proposal.proposal_job_id,
        "request_idempotency_key": proposal.request_idempotency_key,
        "request_binding_digest": proposal.request_binding_digest,
        "acceptance_binding_digest": proposal.acceptance_binding_digest,
        "memory_kind": _m5_export_enum(proposal.memory_kind),
        "memory_scope": scope,
        "preview_text_digest": proposal.preview_text_digest,
        "source_evidence_ids": _m5_export_source_evidence_ids(proposal.source_refs_json),
        "decision_effect": _m5_export_enum(proposal.decision_effect),
        "confidence": proposal.confidence,
        "corrects_memory_id": proposal.corrects_memory_id,
        "reason_code": proposal.reason_code,
        "recovery_action": proposal.recovery_action,
        "provider_contact_started": bool(proposal.provider_contact_started),
        "provider_contact_state": _m5_export_enum(proposal.provider_contact_state),
        "provider_contact_count": proposal.provider_contact_count,
        "privacy_state": _m5_export_enum(proposal.privacy_state),
        "status": _m5_export_enum(proposal.status),
        "accepted_memory_id": proposal.accepted_memory_id,
        "accepted_memory_content_digest": proposal.accepted_memory_content_digest,
        "accepted_by_principal_id": proposal.accepted_by_principal_id,
        "accepted_by_session_id": proposal.accepted_by_session_id,
        "accepted_at": _recovery_timestamp(proposal.accepted_at),
        "rejected_by_principal_id": proposal.rejected_by_principal_id,
        "rejected_by_session_id": proposal.rejected_by_session_id,
        "rejected_at": _recovery_timestamp(proposal.rejected_at),
        "rollback_by_principal_id": proposal.rollback_by_principal_id,
        "rollback_by_session_id": proposal.rollback_by_session_id,
        "rollback_at": _recovery_timestamp(proposal.rollback_at),
        "rollback_reason": proposal.rollback_reason,
        "expires_at": _recovery_timestamp(proposal.expires_at),
        "revision": proposal.revision,
        "created_at": _recovery_timestamp(proposal.created_at),
        "updated_at": _recovery_timestamp(proposal.updated_at),
    }


def _m5_receipt_archive_payload(receipt: WorkBoardDecisionReceipt) -> dict[str, Any]:
    """Serialize the non-content fields needed to recover one decision receipt."""

    return {
        "receipt_id": receipt.receipt_id,
        "schema_version": receipt.schema_version,
        "receipt_stage": _m5_export_enum(receipt.receipt_stage),
        "receipt_binding_digest": receipt.receipt_binding_digest,
        "receipt_integrity_mac": receipt.receipt_integrity_mac,
        "owner_principal_id": receipt.owner_principal_id,
        "owner_session_id": receipt.owner_session_id,
        "source_proposal_id": receipt.source_proposal_id,
        "source_proposal_revision": receipt.source_proposal_revision,
        "source_baseline_receipt_id": receipt.source_baseline_receipt_id,
        "source_task_id": receipt.source_task_id,
        "source_task_revision": receipt.source_task_revision,
        "source_attempt_id": receipt.source_attempt_id,
        "source_attempt_fence": receipt.source_attempt_fence,
        "source_workflow_run_id": receipt.source_workflow_run_id,
        "source_workflow_run_revision": receipt.source_workflow_run_revision,
        "later_task_id": receipt.later_task_id,
        "later_task_revision": receipt.later_task_revision,
        "later_attempt_id": receipt.later_attempt_id,
        "later_workflow_run_id": receipt.later_workflow_run_id,
        "later_attempt_fence": receipt.later_attempt_fence,
        "goal_id": receipt.goal_id,
        "goal_revision": receipt.goal_revision,
        "capability_id": receipt.capability_id,
        "capability_version": receipt.capability_version,
        "typed_input_digest": receipt.typed_input_digest,
        "task_intent_digest": receipt.task_intent_digest,
        "source_context_digest": receipt.source_context_digest,
        "candidate_set_digest": getattr(receipt, "candidate_set_digest", ""),
        "accepted_memory_id": receipt.accepted_memory_id,
        "accepted_memory_content_digest": receipt.accepted_memory_content_digest,
        "before_input_digest": receipt.before_input_digest,
        "after_input_digest": receipt.after_input_digest,
        "before_action_id": receipt.before_action_id,
        "after_action_id": receipt.after_action_id,
        "before_selected_capability_id": receipt.before_selected_capability_id,
        "after_selected_capability_id": receipt.after_selected_capability_id,
        "confirmed_action_id": receipt.confirmed_action_id,
        "comparison_context_digest": receipt.comparison_context_digest,
        "retrieval_evidence_ids": _m5_export_evidence_ids(receipt.retrieval_evidence_ids_json),
        "decision_status": _m5_export_enum(receipt.decision_status),
        "admission_status": _m5_export_enum(receipt.admission_status),
        "reason": receipt.reason,
        "confirmer_principal_id": receipt.confirmer_principal_id,
        "confirmer_session_id": receipt.confirmer_session_id,
        "confirmed_at": _recovery_timestamp(receipt.confirmed_at),
        "confirmation_binding_digest": receipt.confirmation_binding_digest,
        "consumed_at": _recovery_timestamp(receipt.consumed_at),
        "revision": receipt.revision,
        "created_at": _recovery_timestamp(receipt.created_at),
        "updated_at": _recovery_timestamp(receipt.updated_at),
    }


def _m5_normalize_owner(value: Any, *, field_name: str, expected: str) -> str:
    actual = _m5_recovery_id(value, field_name=field_name, required=True)
    if actual != expected:
        raise PermissionError(f"memory restore M5 {field_name} does not match the authenticated owner")
    return actual


def _m5_normalize_optional_id(value: Any, *, field_name: str) -> str | None:
    return _m5_recovery_id(value, field_name=field_name)


def _m5_normalize_optional_digest(value: Any, *, field_name: str) -> str | None:
    return _m5_recovery_digest(value, field_name=field_name)


def _m5_normalize_proposal_record(
    record: Any,
    *,
    owner_principal_id: str,
    owner_session_id: str,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("memory restore archive contains an invalid M5 proposal")
    proposal_id = _m5_recovery_id(record.get("proposal_id"), field_name="proposal_id", required=True)
    recovered_from_proposal_id = _m5_normalize_optional_id(
        record.get("recovered_from_proposal_id"),
        field_name="recovered_from_proposal_id",
    )
    if recovered_from_proposal_id == proposal_id:
        raise ValueError(f"memory restore M5 proposal {proposal_id} cannot recover from itself")
    if record.get("schema_version") != "memory_proposal.v1":
        raise ValueError(f"memory restore M5 proposal {proposal_id} has an invalid schema version")
    _m5_normalize_owner(record.get("owner_principal_id"), field_name="proposal owner principal", expected=owner_principal_id)
    _m5_normalize_owner(record.get("owner_session_id"), field_name="proposal owner session", expected=owner_session_id)
    source_task_id = _m5_recovery_id(record.get("source_task_id"), field_name="source_task_id", required=True)
    source_attempt_id = _m5_recovery_id(record.get("source_attempt_id"), field_name="source_attempt_id", required=True)
    workflow_run_id = _m5_recovery_id(record.get("workflow_run_id"), field_name="workflow_run_id")
    goal_id = _m5_recovery_id(record.get("goal_id"), field_name="goal_id", required=True)
    goal_revision = _m5_recovery_int(record.get("goal_revision"), field_name="goal_revision", minimum=1)
    capability_id = _m5_recovery_id(record.get("capability_id"), field_name="capability_id", required=True)
    typed_input_digest = _m5_recovery_digest(
        record.get("typed_input_digest"), field_name="typed_input_digest"
    ) or ""
    source_context_digest = _m5_recovery_digest(
        record.get("source_context_digest"), field_name="source_context_digest", required=True
    )
    candidate_set_digest = _m5_recovery_digest(
        record.get("candidate_set_digest"), field_name="candidate_set_digest"
    ) or ""
    request_binding_digest = _m5_recovery_digest(
        record.get("request_binding_digest"), field_name="request_binding_digest", required=True
    )
    accepted_memory_id = _m5_normalize_optional_id(
        record.get("accepted_memory_id"), field_name="accepted_memory_id"
    )
    accepted_memory_digest = _m5_normalize_optional_digest(
        record.get("accepted_memory_content_digest"), field_name="accepted_memory_content_digest"
    )
    if bool(accepted_memory_id) != bool(accepted_memory_digest):
        raise ValueError(f"memory restore M5 proposal {proposal_id} has an incomplete accepted memory binding")
    accepted_by_principal_id = _m5_normalize_optional_id(
        record.get("accepted_by_principal_id"), field_name="accepted_by_principal_id"
    )
    accepted_by_session_id = _m5_normalize_optional_id(
        record.get("accepted_by_session_id"), field_name="accepted_by_session_id"
    )
    for field_name, value, expected in (
        ("accepted_by_principal_id", accepted_by_principal_id, owner_principal_id),
        ("accepted_by_session_id", accepted_by_session_id, owner_session_id),
    ):
        if value and value != expected:
            raise PermissionError(f"memory restore M5 proposal {proposal_id} {field_name} does not match the owner")
    rejected_by_principal_id = _m5_normalize_optional_id(
        record.get("rejected_by_principal_id"), field_name="rejected_by_principal_id"
    )
    rejected_by_session_id = _m5_normalize_optional_id(
        record.get("rejected_by_session_id"), field_name="rejected_by_session_id"
    )
    rollback_by_principal_id = _m5_normalize_optional_id(
        record.get("rollback_by_principal_id"), field_name="rollback_by_principal_id"
    )
    rollback_by_session_id = _m5_normalize_optional_id(
        record.get("rollback_by_session_id"), field_name="rollback_by_session_id"
    )
    for field_name, value, expected in (
        ("rejected_by_principal_id", rejected_by_principal_id, owner_principal_id),
        ("rejected_by_session_id", rejected_by_session_id, owner_session_id),
        ("rollback_by_principal_id", rollback_by_principal_id, owner_principal_id),
        ("rollback_by_session_id", rollback_by_session_id, owner_session_id),
    ):
        if value and value != expected:
            raise PermissionError(f"memory restore M5 proposal {proposal_id} {field_name} does not match the owner")
    status = _m5_recovery_enum(record.get("status"), MemoryProposalStatus, field_name="proposal status")
    privacy_state = _m5_recovery_enum(
        record.get("privacy_state"), MemoryProposalPrivacyState, field_name="proposal privacy state"
    )
    memory_kind = record.get("memory_kind")
    normalized_memory_kind = _m5_recovery_enum(memory_kind, MemoryKind, field_name="proposal memory kind") if memory_kind else None
    if normalized_memory_kind is not None and normalized_memory_kind not in {MemoryKind.fact, MemoryKind.pattern}:
        raise ValueError(f"memory restore M5 proposal {proposal_id} has an invalid memory kind")
    decision_effect = _m5_recovery_enum(
        record.get("decision_effect"), MemoryProposalDecisionEffect, field_name="proposal decision effect"
    )
    provider_contact_state = _m5_recovery_enum(
        record.get("provider_contact_state"),
        MemoryProposalProviderContactState,
        field_name="proposal provider contact state",
    )
    confidence = record.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool):
            raise ValueError(f"memory restore M5 proposal {proposal_id} confidence is invalid")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"memory restore M5 proposal {proposal_id} confidence is invalid") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError(f"memory restore M5 proposal {proposal_id} confidence is invalid")
    created_at = _m5_recovery_timestamp(record.get("created_at"), field_name="proposal.created_at", required=True)
    updated_at = _m5_recovery_timestamp(record.get("updated_at"), field_name="proposal.updated_at", required=True)
    provider_contact_started = record.get("provider_contact_started", False)
    if not isinstance(provider_contact_started, bool):
        raise ValueError(f"memory restore M5 proposal {proposal_id} provider contact flag is invalid")
    scope_json, scope_valid = _m5_recovery_scope(
        record.get("memory_scope"),
        require_preferred_version=(
            decision_effect is MemoryProposalDecisionEffect.require_operator_confirmation
        ),
    )
    if scope_valid:
        scope = json.loads(scope_json or "{}")
        if (
            scope.get("goal_id") != goal_id
            or int(scope.get("goal_revision") or 0) != goal_revision
            or scope.get("source_context_digest") != source_context_digest
        ):
            scope_json = None
            scope_valid = False
    if status is MemoryProposalStatus.accepted and not scope_valid:
        # A legacy accepted row without the bounded matching scope must remain
        # visible for recovery, but it cannot become a later decision source.
        status = MemoryProposalStatus.blocked
        reason_code = "memory_scope_not_restored"
        recovery_action = "request_verified_proposal_again"
    else:
        reason_code = _m5_recovery_text(record.get("reason_code"), field_name="reason_code")
        recovery_action = _m5_recovery_text(record.get("recovery_action"), field_name="recovery_action")
    if status is MemoryProposalStatus.proposed:
        # Proposal text is intentionally never exported.  Do not recreate an
        # apparently actionable proposal with a digest but no review content.
        status = MemoryProposalStatus.blocked
        reason_code = "proposal_preview_not_restored"
        recovery_action = "request_verified_proposal_again"
    return {
        "proposal_id": proposal_id,
        "recovered_from_proposal_id": recovered_from_proposal_id,
        "schema_version": "memory_proposal.v1",
        "owner_principal_id": owner_principal_id,
        "owner_session_id": owner_session_id,
        "source_task_id": source_task_id,
        "source_task_revision": _m5_recovery_int(record.get("source_task_revision"), field_name="source_task_revision", minimum=1),
        "source_attempt_id": source_attempt_id,
        "source_attempt_fence": _m5_recovery_int(record.get("source_attempt_fence"), field_name="source_attempt_fence"),
        "workflow_run_id": workflow_run_id or "",
        "workflow_run_revision": _m5_recovery_int(record.get("workflow_run_revision"), field_name="workflow_run_revision"),
        "goal_id": goal_id,
        "goal_revision": goal_revision,
        "capability_id": capability_id,
        "capability_version": _m5_recovery_text(record.get("capability_version"), field_name="capability_version", maximum=255),
        "typed_input_digest": typed_input_digest,
        "source_context_digest": source_context_digest,
        "candidate_set_digest": candidate_set_digest,
        "evidence_digest": _m5_normalize_optional_digest(record.get("evidence_digest"), field_name="evidence_digest"),
        "readback_kind": _m5_recovery_text(record.get("readback_kind"), field_name="readback_kind", maximum=255),
        "readback_ref": _m5_recovery_text(record.get("readback_ref"), field_name="readback_ref", maximum=512) or None,
        "readback_digest": _m5_normalize_optional_digest(record.get("readback_digest"), field_name="readback_digest"),
        "artifact_ref": _m5_recovery_text(record.get("artifact_ref"), field_name="artifact_ref", maximum=512) or None,
        "artifact_digest": _m5_normalize_optional_digest(record.get("artifact_digest"), field_name="artifact_digest"),
        "proposal_job_id": _m5_normalize_optional_id(record.get("proposal_job_id"), field_name="proposal_job_id"),
        "request_idempotency_key": _m5_recovery_text(record.get("request_idempotency_key"), field_name="request_idempotency_key", maximum=512),
        "request_binding_digest": request_binding_digest,
        "acceptance_binding_digest": _m5_normalize_optional_digest(record.get("acceptance_binding_digest"), field_name="acceptance_binding_digest"),
        "memory_kind": normalized_memory_kind,
        # Only the allowlisted matching scope survives recovery.  Owner and
        # session are always taken from the authenticated restore context.
        "memory_scope_json": scope_json,
        "preview_text": None,
        "preview_text_digest": _m5_normalize_optional_digest(record.get("preview_text_digest"), field_name="preview_text_digest"),
        "decision_effect": decision_effect,
        "confidence": confidence,
        "corrects_memory_id": _m5_normalize_optional_id(record.get("corrects_memory_id"), field_name="corrects_memory_id"),
        "provenance_json": "{}",
        "source_refs_json": _m5_recovery_evidence_ids(
            record.get("source_evidence_ids"),
            field_name="proposal source evidence ids",
            maximum=_M5_SOURCE_EVIDENCE_LIMIT,
        ),
        "reason_code": reason_code,
        "recovery_action": recovery_action,
        "provider_contact_started": provider_contact_started,
        "provider_contact_state": provider_contact_state,
        "provider_contact_count": _m5_recovery_int(record.get("provider_contact_count"), field_name="provider_contact_count", maximum=10000),
        "privacy_state": privacy_state,
        "status": status,
        "accepted_memory_id": accepted_memory_id,
        "accepted_memory_content_digest": accepted_memory_digest,
        "accepted_by_principal_id": accepted_by_principal_id,
        "accepted_by_session_id": accepted_by_session_id,
        "accepted_at": _m5_recovery_timestamp(record.get("accepted_at"), field_name="proposal.accepted_at"),
        "rejected_by_principal_id": rejected_by_principal_id,
        "rejected_by_session_id": rejected_by_session_id,
        "rejected_at": _m5_recovery_timestamp(record.get("rejected_at"), field_name="proposal.rejected_at"),
        "rollback_by_principal_id": rollback_by_principal_id,
        "rollback_by_session_id": rollback_by_session_id,
        "rollback_at": _m5_recovery_timestamp(record.get("rollback_at"), field_name="proposal.rollback_at"),
        "rollback_reason": _m5_recovery_text(record.get("rollback_reason"), field_name="proposal.rollback_reason", maximum=500),
        "expires_at": _m5_recovery_timestamp(record.get("expires_at"), field_name="proposal.expires_at"),
        "revision": _m5_recovery_int(record.get("revision"), field_name="revision", minimum=1),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _m5_normalize_receipt_record(
    record: Any,
    *,
    owner_principal_id: str,
    owner_session_id: str,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("memory restore archive contains an invalid M5 decision receipt")
    receipt_id = _m5_recovery_id(record.get("receipt_id"), field_name="receipt_id", required=True)
    if record.get("schema_version") != "work_board_decision_receipt.v1":
        raise ValueError(f"memory restore M5 receipt {receipt_id} has an invalid schema version")
    _m5_normalize_owner(record.get("owner_principal_id"), field_name="receipt owner principal", expected=owner_principal_id)
    _m5_normalize_owner(record.get("owner_session_id"), field_name="receipt owner session", expected=owner_session_id)
    receipt_binding_digest = _m5_recovery_digest(
        record.get("receipt_binding_digest"), field_name="receipt_binding_digest", required=True
    )
    accepted_memory_id = _m5_normalize_optional_id(record.get("accepted_memory_id"), field_name="receipt accepted_memory_id")
    accepted_memory_digest = _m5_normalize_optional_digest(
        record.get("accepted_memory_content_digest"), field_name="receipt accepted_memory_content_digest"
    )
    if bool(accepted_memory_id) != bool(accepted_memory_digest):
        raise ValueError(f"memory restore M5 receipt {receipt_id} has an incomplete accepted memory binding")
    created_at = _m5_recovery_timestamp(record.get("created_at"), field_name="receipt.created_at", required=True)
    updated_at = _m5_recovery_timestamp(record.get("updated_at"), field_name="receipt.updated_at", required=True)
    digest_fields = (
        "typed_input_digest",
        "task_intent_digest",
        "source_context_digest",
        "candidate_set_digest",
        "before_input_digest",
        "after_input_digest",
        "comparison_context_digest",
    )
    normalized_digests = {
        field: _m5_recovery_digest(record.get(field), field_name=f"receipt.{field}") or ""
        for field in digest_fields
    }
    confirmer_principal_id = _m5_normalize_optional_id(
        record.get("confirmer_principal_id"), field_name="confirmer_principal_id"
    )
    confirmer_session_id = _m5_normalize_optional_id(
        record.get("confirmer_session_id"), field_name="confirmer_session_id"
    )
    if confirmer_principal_id and confirmer_principal_id != owner_principal_id:
        raise PermissionError(f"memory restore M5 receipt {receipt_id} confirmer principal does not match the owner")
    if confirmer_session_id and confirmer_session_id != owner_session_id:
        raise PermissionError(f"memory restore M5 receipt {receipt_id} confirmer session does not match the owner")
    return {
        "receipt_id": receipt_id,
        "schema_version": "work_board_decision_receipt.v1",
        "receipt_stage": _m5_recovery_enum(record.get("receipt_stage"), WorkBoardDecisionReceiptStage, field_name="receipt stage"),
        "receipt_binding_digest": receipt_binding_digest,
        # This is optional only for legacy archives.  Restore never treats a
        # missing or invalid value as authenticated; the receipt/proposal is
        # quarantined below until the operator re-verifies and accepts it.
        "receipt_integrity_mac": _m5_normalize_optional_digest(
            record.get("receipt_integrity_mac"), field_name="receipt_integrity_mac"
        ),
        "owner_principal_id": owner_principal_id,
        "owner_session_id": owner_session_id,
        "source_proposal_id": _m5_normalize_optional_id(record.get("source_proposal_id"), field_name="source_proposal_id"),
        "source_proposal_revision": _m5_recovery_int(record.get("source_proposal_revision"), field_name="source_proposal_revision"),
        "source_baseline_receipt_id": _m5_normalize_optional_id(record.get("source_baseline_receipt_id"), field_name="source_baseline_receipt_id"),
        "source_task_id": _m5_normalize_optional_id(record.get("source_task_id"), field_name="source_task_id"),
        "source_task_revision": _m5_recovery_int(record.get("source_task_revision"), field_name="source_task_revision"),
        "source_attempt_id": _m5_normalize_optional_id(record.get("source_attempt_id"), field_name="source_attempt_id"),
        "source_attempt_fence": _m5_recovery_int(record.get("source_attempt_fence"), field_name="source_attempt_fence"),
        "source_workflow_run_id": _m5_normalize_optional_id(record.get("source_workflow_run_id"), field_name="source_workflow_run_id"),
        "source_workflow_run_revision": _m5_recovery_int(record.get("source_workflow_run_revision"), field_name="source_workflow_run_revision"),
        "later_task_id": _m5_recovery_id(record.get("later_task_id"), field_name="later_task_id", required=True),
        "later_task_revision": _m5_recovery_int(record.get("later_task_revision"), field_name="later_task_revision", minimum=1),
        "later_attempt_id": _m5_normalize_optional_id(record.get("later_attempt_id"), field_name="later_attempt_id"),
        "later_workflow_run_id": _m5_normalize_optional_id(record.get("later_workflow_run_id"), field_name="later_workflow_run_id"),
        "later_attempt_fence": _m5_recovery_int(record.get("later_attempt_fence"), field_name="later_attempt_fence"),
        "goal_id": _m5_recovery_id(record.get("goal_id"), field_name="receipt.goal_id", required=True),
        "goal_revision": _m5_recovery_int(record.get("goal_revision"), field_name="receipt.goal_revision", minimum=1),
        "capability_id": _m5_recovery_id(record.get("capability_id"), field_name="receipt.capability_id", required=True),
        "capability_version": _m5_recovery_text(record.get("capability_version"), field_name="receipt.capability_version", maximum=255),
        **normalized_digests,
        "accepted_memory_id": accepted_memory_id,
        "accepted_memory_content_digest": accepted_memory_digest,
        "before_action_id": _m5_recovery_text(record.get("before_action_id"), field_name="receipt.before_action_id", maximum=512),
        "after_action_id": _m5_recovery_text(record.get("after_action_id"), field_name="receipt.after_action_id", maximum=512),
        "before_selected_capability_id": _m5_normalize_optional_id(record.get("before_selected_capability_id"), field_name="before_selected_capability_id"),
        "after_selected_capability_id": _m5_normalize_optional_id(record.get("after_selected_capability_id"), field_name="after_selected_capability_id"),
        "confirmed_action_id": _m5_recovery_text(record.get("confirmed_action_id"), field_name="receipt.confirmed_action_id", maximum=512) or None,
        "retrieval_evidence_ids_json": _m5_recovery_evidence_ids(record.get("retrieval_evidence_ids", [])),
        "decision_status": _m5_recovery_enum(record.get("decision_status"), WorkBoardDecisionStatus, field_name="receipt decision status"),
        "admission_status": _m5_recovery_enum(record.get("admission_status"), WorkBoardDecisionAdmissionStatus, field_name="receipt admission status"),
        "reason": _m5_recovery_text(record.get("reason"), field_name="receipt.reason"),
        "confirmer_principal_id": confirmer_principal_id,
        "confirmer_session_id": confirmer_session_id,
        "confirmed_at": _m5_recovery_timestamp(record.get("confirmed_at"), field_name="receipt.confirmed_at"),
        "confirmation_binding_digest": _m5_normalize_optional_digest(record.get("confirmation_binding_digest"), field_name="confirmation_binding_digest"),
        "consumed_at": _m5_recovery_timestamp(record.get("consumed_at"), field_name="receipt.consumed_at"),
        "revision": _m5_recovery_int(record.get("revision"), field_name="receipt.revision", minimum=1),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _memory_export_integrity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable export envelope covered by ``export_hash``.

    ``artifact_path`` is derived from the export hash and ``artifact_sha256``
    is the checksum of the complete persisted payload, so neither can be part
    of the export-hash input without creating a circular value.
    """

    return {
        key: payload[key]
        for key in _MEMORY_EXPORT_ENVELOPE_FIELDS
        if key in payload
    }


def _memory_export_artifact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the persisted export payload covered by ``artifact_sha256``."""

    return {
        key: value
        for key, value in payload.items()
        if key != "artifact_sha256"
    }


def _bounded_recovery_float(
    value: Any,
    *,
    default: float,
    field_name: str,
    memory_id: str,
    minimum: float = 0.0,
    maximum: float | None = 1.0,
) -> float:
    """Validate numeric archive fields before any restore write occurs."""

    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"memory restore record {memory_id} has invalid {field_name}"
        ) from exc
    if not math.isfinite(parsed):
        raise ValueError(f"memory restore record {memory_id} has invalid {field_name}")
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _recovery_authority(
    *,
    actor: str,
    owner_session_id: str | None,
    authenticated_session_id: str | None,
    source_role: str,
) -> tuple[str, str]:
    """Resolve and validate the current runtime operator authority.

    Recovery is a privileged store operation, so request arguments are only
    an envelope to compare with the identity already bound to the current
    execution.  A caller that supplies an actor/session without a runtime
    principal is rejected before any database or artifact work occurs.
    """

    normalized_actor = str(actor or "").strip()
    normalized_owner = str(owner_session_id or "").strip()
    normalized_authenticated = str(authenticated_session_id or "").strip()
    normalized_role = str(source_role or "").strip().lower()

    # The API middleware verifies the operator session before binding this
    # principal.  Check the runtime cancellation guard as well so an in-flight
    # request cannot continue recovery after that session is revoked.
    try:
        assert_runtime_not_revoked()
    except PermissionError as exc:
        raise PermissionError(str(exc)) from exc
    principal = get_current_trust_principal()
    runtime_session = str(get_current_session_id() or "").strip()
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    principal_type = getattr(getattr(principal, "principal_type", None), "value", None) or str(
        getattr(principal, "principal_type", "") or ""
    ).strip().lower()
    principal_session = str(getattr(principal, "session_id", "") or "").strip()
    operator_session = str(getattr(principal, "operator_session_id", "") or "").strip()
    grants = {
        str(getattr(grant, "value", grant)).strip()
        for grant in getattr(principal, "grants", ())
    }
    if (
        principal is None
        or principal_type != PrincipalType.OPERATOR.value
        or not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or not principal_id
        or not runtime_session
        or principal_session != runtime_session
        or operator_session != runtime_session
        or AuthorityGrant.CAPABILITY_EXECUTE.value not in grants
    ):
        raise PermissionError("memory recovery requires a current authenticated operator runtime")
    if not normalized_actor or normalized_actor != principal_id:
        raise PermissionError("memory recovery actor does not match the authenticated principal")
    if not normalized_owner or not normalized_authenticated:
        raise PermissionError("memory recovery requires an authenticated owner session")
    if normalized_owner != runtime_session or normalized_authenticated != runtime_session:
        raise PermissionError("memory owner session does not match the authenticated session")
    if normalized_role != "operator":
        raise PermissionError("memory recovery source role must be operator")
    return principal_id, runtime_session


def _recovery_artifact_path(*, kind: str, digest: str) -> tuple[Path, str]:
    """Resolve a derived/canonical artifact through the workspace registry."""

    try:
        root = canonical_workspace_root(settings.workspace_dir)
        registry = canonical_workspace_registry(root)
        logical_root = "artifacts" if kind == "export" else "cache"
        expected_class = (
            WorkspaceStateClass.CANONICAL
            if logical_root == "artifacts"
            else WorkspaceStateClass.CACHE
        )
        if registry.classify_path(logical_root) is not expected_class:
            raise RuntimeError(f"memory recovery {kind} path is not workspace-owned")
        logical_directory = root / logical_root
        try:
            logical_stat = logical_directory.lstat()
        except FileNotFoundError:
            logical_stat = None
        if logical_stat is not None and (
            stat.S_ISLNK(logical_stat.st_mode) or not stat.S_ISDIR(logical_stat.st_mode)
        ):
            raise RuntimeError(f"memory recovery {kind} workspace root is not a regular directory")
        logical_directory.mkdir(parents=False, exist_ok=True)
        if stat.S_ISLNK(logical_directory.lstat().st_mode) or not stat.S_ISDIR(logical_directory.stat().st_mode):
            raise RuntimeError(f"memory recovery {kind} workspace root is not a regular directory")
        directory = root / logical_root / "memory-recovery"
        # Check a replaced nested path before mkdir follows a possible link.
        try:
            directory_stat = directory.lstat()
        except FileNotFoundError:
            directory_stat = None
        if directory_stat is not None and (
            stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode)
        ):
            raise RuntimeError(f"memory recovery {kind} path is not a regular directory")
        directory.mkdir(parents=True, exist_ok=True)
        # The registry owns the top-level path.  Refuse a replaced nested
        # directory before writing a recovery artifact into it.
        if stat.S_ISLNK(directory.lstat().st_mode) or not stat.S_ISDIR(directory.stat().st_mode):
            raise RuntimeError(f"memory recovery {kind} path is not a regular directory")
        filename = f"{kind}-{digest[:24]}.json"
        return directory / filename, f"{logical_root}/memory-recovery/{filename}"
    except (WorkspaceStateError, OSError) as exc:
        raise RuntimeError(f"memory recovery {kind} workspace path is unavailable") from exc


def _write_recovery_artifact(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write one bounded JSON recovery artifact with private mode."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    parent = path.parent
    if path.exists() or path.is_symlink():
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise RuntimeError("memory recovery artifact path is not a regular file")
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
    except OSError as exc:
        raise RuntimeError("memory recovery artifact workspace is not writable") from exc
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("memory recovery artifact could not be committed") from exc
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _archive_memory_payload(memory: Memory, sources: list[MemorySource]) -> dict[str, Any]:
    try:
        parsed_metadata = json.loads(memory.metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed_metadata = {}
    metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
    return {
        "id": memory.id,
        "content": memory.content,
        "category": _coerce_enum(memory.category, MemoryCategory).value,
        "kind": _coerce_enum(memory.kind, MemoryKind).value,
        "summary": memory.summary,
        "confidence": float(memory.confidence or 0.0),
        "importance": float(memory.importance or 0.0),
        "reinforcement": float(memory.reinforcement or 0.0),
        "status": _coerce_enum(memory.status, MemoryStatus).value,
        "subject_entity_id": memory.subject_entity_id,
        "project_entity_id": memory.project_entity_id,
        "source_session_id": memory.source_session_id,
        "embedding_id": memory.embedding_id,
        "scope_key": memory.scope_key,
        "metadata": metadata,
        "created_at": _recovery_timestamp(memory.created_at),
        "updated_at": _recovery_timestamp(memory.updated_at),
        "last_confirmed_at": _recovery_timestamp(memory.last_confirmed_at),
        "sources": [
            {
                "id": source.id,
                "source_type": source.source_type,
                "source_session_id": source.source_session_id,
                "source_message_id": source.source_message_id,
                "snippet": source.snippet,
                "created_at": _recovery_timestamp(source.created_at),
            }
            for source in sources
        ],
    }


def _canonical_memory_without_tombstone_clause():
    """Return the SQL predicate for a memory with no durable delete authority."""

    return ~exists().where(MemoryTombstone.memory_id == Memory.id)


def _canonical_memory_is_active(memory: Memory) -> bool:
    try:
        return _coerce_enum(memory.status, MemoryStatus) is MemoryStatus.active
    except (TypeError, ValueError):
        return False


def _canonical_memory_deletion_marker(memory: Memory) -> str | None:
    """Return a durable canonical delete marker, if one is present.

    Canonical delete/export is terminal for the local memory record. Read both
    the current nested operator-control fields and older top-level markers so
    provider or learning ingress cannot revive a tombstone after a metadata
    shape migration. Malformed metadata on an archived or superseded row fails
    closed because it cannot disprove that a canonical deletion marker exists;
    an active malformed row remains an ordinary writable record.
    """

    raw_metadata = getattr(memory, "metadata_json", None)
    metadata_malformed = False
    try:
        parsed_metadata = json.loads(raw_metadata or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed_metadata = {}
        metadata_malformed = bool(raw_metadata)
    metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
    if not isinstance(parsed_metadata, dict):
        metadata_malformed = True

    operator_control_value = metadata.get("operator_control")
    if operator_control_value is None:
        operator_control: dict[str, Any] = {}
    elif isinstance(operator_control_value, dict):
        operator_control = operator_control_value
    else:
        operator_control = {}
        metadata_malformed = True

    def _marker_value(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().lower()

    archived_reason = _marker_value(metadata.get("archived_reason"))
    if "archived_reason" in metadata and not isinstance(metadata.get("archived_reason"), str):
        metadata_malformed = True
    if archived_reason == _CANONICAL_MEMORY_DELETE_EXPORT_REASON:
        return f"archived_reason={archived_reason}"

    delete_export_state_value = operator_control.get("delete_export_state")
    top_level_delete_export_state = metadata.get("delete_export_state")
    if "delete_export_state" in operator_control and not isinstance(delete_export_state_value, str):
        metadata_malformed = True
    if "delete_export_state" in metadata and not isinstance(top_level_delete_export_state, str):
        metadata_malformed = True
    for delete_export_state in (
        _marker_value(delete_export_state_value),
        _marker_value(top_level_delete_export_state),
    ):
        if delete_export_state == _CANONICAL_MEMORY_REDACTED_STATE:
            return f"delete_export_state={delete_export_state}"

    last_action_value = operator_control.get("last_action")
    if "last_action" in operator_control and not isinstance(last_action_value, str):
        metadata_malformed = True
    top_level_last_action = metadata.get("last_action")
    if "last_action" in metadata and not isinstance(top_level_last_action, str):
        metadata_malformed = True
    for last_action in (
        _marker_value(last_action_value),
        _marker_value(top_level_last_action),
    ):
        if last_action in _CANONICAL_MEMORY_DELETE_ACTIONS:
            return f"last_action={last_action}"

    # The propagated replacement is itself a canonical redaction marker. Keep
    # this fallback for records written before the explicit state fields.
    content = str(getattr(memory, "content", "") or "").strip()
    summary = str(getattr(memory, "summary", "") or "").strip()
    if content == _CANONICAL_MEMORY_DELETE_CONTENT:
        return "content=canonical_memory_redacted"
    if summary == _CANONICAL_MEMORY_DELETE_CONTENT:
        return "summary=canonical_memory_redacted"

    if metadata_malformed:
        status = getattr(memory, "status", None)
        try:
            normalized_status = _coerce_enum(status, MemoryStatus).value
        except (TypeError, ValueError):
            normalized_status = str(status or "").strip().lower()
        if normalized_status in {MemoryStatus.archived.value, MemoryStatus.superseded.value}:
            return "metadata=malformed_suppressed_memory"

    return None


def _m5_rollback_marker_from_memory(value: Any) -> dict[str, Any] | None:
    """Return a parsed durable M5 rollback marker from a memory payload."""

    metadata = value
    if isinstance(value, Memory):
        try:
            metadata = json.loads(value.metadata_json or "{}")
        except (TypeError, ValueError):
            return None
    elif isinstance(value, dict):
        metadata = value.get("metadata", value.get("metadata_json", value))
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata or "{}")
            except (TypeError, ValueError):
                return None
    if not isinstance(metadata, dict):
        return None
    marker = metadata.get("work_board_rollback")
    return marker if isinstance(marker, dict) else None


def _m5_rollback_proposal_id_from_memory(value: Any) -> str | None:
    """Return the proposal identifier from a durable rollback marker.

    Current database rows retain this identifier as a rollback barrier for
    compatibility with the canonical rollback audit.  Archive restore uses
    ``_m5_rollback_marker_state`` below before treating the marker as
    authoritative because public archive hashes are recomputable.
    """

    marker = _m5_rollback_marker_from_memory(value)
    if marker is None:
        return None
    proposal_id = marker.get("proposal_id")
    if not isinstance(proposal_id, str):
        return None
    proposal_id = proposal_id.strip()
    return proposal_id or None


def _m5_rollback_marker_state(
    value: Any,
    *,
    memory_id: str,
    proposal_id: str,
    accepted_content_digest: str,
    owner_principal_id: str,
    owner_session_id: str,
) -> str:
    """Classify an archive rollback marker without trusting public hashes.

    ``mismatch`` means a structurally complete marker carries the active key
    identifier but its signed identity does not match.  Such a marker is
    ignored so a rehashed archive cannot change an active memory's lifecycle.
    ``unverifiable`` means the marker is legacy, malformed, or cannot be
    checked with the active key.  Restore quarantines that memory/proposal and
    requires explicit re-verification instead of activating it.
    """

    marker = _m5_rollback_marker_from_memory(value)
    if marker is None:
        return "absent"
    if marker.get("version") != _M5_ROLLBACK_BINDING_SCHEMA_VERSION:
        return "unverifiable"
    required_fields = (
        "memory_id",
        "proposal_id",
        "accepted_content_digest",
        "owner_principal_id",
        "owner_session_id",
        "rollback_at",
        "key_id",
        "binding_mac",
    )
    if any(not isinstance(marker.get(field), str) or not marker[field].strip() for field in required_fields):
        return "unverifiable"
    marker_values = {
        field: marker[field].strip()
        for field in required_fields
        if field != "binding_mac"
    }
    rollback_reason = marker.get("rollback_reason")
    if not isinstance(rollback_reason, str) or len(rollback_reason) > 500:
        return "unverifiable"
    marker_values["rollback_reason"] = rollback_reason
    marker_values["accepted_content_digest"] = marker_values[
        "accepted_content_digest"
    ].lower()
    marker_values["binding_mac"] = marker["binding_mac"].strip().lower()
    if not _M5_RECOVERY_DIGEST.fullmatch(marker_values["accepted_content_digest"]):
        return "unverifiable"
    if not re.fullmatch(r"[0-9a-f]{24}", marker_values["key_id"]):
        return "unverifiable"
    if not _M5_RECOVERY_DIGEST.fullmatch(marker_values["binding_mac"]):
        return "unverifiable"
    try:
        parsed_rollback_at = _parse_recovery_timestamp(
            marker_values["rollback_at"],
            field_name="work_board_rollback.rollback_at",
        )
        active_key_id = _m5_rollback_binding_key_id()
    except (CapabilityJournalError, TypeError, ValueError):
        return "unverifiable"
    normalized_rollback_at = _recovery_timestamp(parsed_rollback_at)
    if normalized_rollback_at is None:
        return "unverifiable"
    legacy_at = marker.get("at")
    if legacy_at is not None:
        try:
            normalized_legacy_at = _recovery_timestamp(
                _parse_recovery_timestamp(
                    legacy_at,
                    field_name="work_board_rollback.at",
                )
            )
        except (TypeError, ValueError):
            return "unverifiable"
        if normalized_legacy_at != normalized_rollback_at:
            return "mismatch"
    if marker_values["key_id"] != active_key_id:
        return "unverifiable"
    expected_identity = {
        "memory_id": str(memory_id or "").strip(),
        "proposal_id": str(proposal_id or "").strip(),
        "accepted_content_digest": str(accepted_content_digest or "").strip().lower(),
        "owner_principal_id": str(owner_principal_id or "").strip(),
        "owner_session_id": str(owner_session_id or "").strip(),
    }
    for field, expected in expected_identity.items():
        if marker_values[field] != expected:
            return "mismatch"
    try:
        expected_mac = _m5_rollback_binding_mac(
            memory_id=marker_values["memory_id"],
            proposal_id=marker_values["proposal_id"],
            accepted_content_digest=marker_values["accepted_content_digest"],
            owner_principal_id=marker_values["owner_principal_id"],
            owner_session_id=marker_values["owner_session_id"],
            rollback_at=normalized_rollback_at,
            rollback_reason=marker_values["rollback_reason"],
        )
    except (CapabilityJournalError, TypeError, ValueError):
        return "unverifiable"
    return (
        "valid"
        if hmac.compare_digest(marker_values["binding_mac"], expected_mac)
        else "mismatch"
    )


def _sqlite_json_object_path(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"$.{key}"
    escaped_key = key.replace('"', '\\"')
    return f'$."{escaped_key}"'


@dataclass(frozen=True)
class MemoryWriteResult:
    memory_id: str
    subject_entity_id: str | None = None
    project_entity_id: str | None = None
    message_source_count: int = 0
    session_source_created: bool = False


@dataclass(frozen=True)
class MemorySourceWriteResult:
    source_id: str
    created: bool


@dataclass(frozen=True)
class MemoryTombstoneWriteResult:
    memory: Memory
    tombstone: MemoryTombstone
    created: bool


class MemoryRepository:
    def __init__(self) -> None:
        self._scoped_memory_locks: dict[str, asyncio.Lock] = {}
        self._canonical_memory_lock = asyncio.Lock()

    @staticmethod
    def _normalize_memory_text(value: str | None) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.strip().lower().split())

    @staticmethod
    def _normalize_source_snippet(value: str | None) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return " ".join(value.strip().split())[:240]

    def _get_scoped_memory_lock(
        self,
        *,
        kind: MemoryKind,
        scope: dict[str, Any],
    ) -> asyncio.Lock:
        key = json.dumps(
            {"kind": kind.value, "scope": scope},
            sort_keys=True,
            default=str,
        )
        lock = self._scoped_memory_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._scoped_memory_locks[key] = lock
        return lock

    @staticmethod
    def _scoped_memory_key(
        *,
        kind: MemoryKind,
        scope: dict[str, Any],
    ) -> str:
        return json.dumps(
            {"kind": kind.value, "scope": scope},
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _normalize_episode_kwargs(
        *,
        episode_type: MemoryEpisodeType | str,
        summary: str,
        content: str,
        session_id: str | None,
        source_message_id: str | None,
        source_tool_name: str | None,
        source_role: str | None,
        subject_entity_id: str | None,
        project_entity_id: str | None,
        salience: float,
        confidence: float,
        metadata: dict[str, Any] | None,
        observed_at: datetime | None,
    ) -> dict[str, Any]:
        normalized_summary = summary.strip()
        normalized_content = content.strip()
        if not normalized_summary:
            raise ValueError("summary must be non-empty")
        if not normalized_content:
            raise ValueError("content must be non-empty")
        normalized_episode_type = _coerce_enum(episode_type, MemoryEpisodeType)
        return {
            "session_id": session_id,
            "episode_type": normalized_episode_type,
            "summary": normalized_summary,
            "content": normalized_content,
            "source_message_id": source_message_id,
            "source_tool_name": source_tool_name,
            "source_role": source_role,
            "subject_entity_id": subject_entity_id,
            "project_entity_id": project_entity_id,
            "salience": salience,
            "confidence": confidence,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
            "observed_at": observed_at or _now(),
        }

    async def get_or_create_entity(
        self,
        *,
        canonical_name: str,
        entity_type: MemoryEntityType | str = MemoryEntityType.person,
        aliases: list[str] | None = None,
    ) -> MemoryEntity:
        normalized_name = canonical_name.strip()
        if not normalized_name:
            raise ValueError("canonical_name must be non-empty")
        normalized_entity_type = _coerce_enum(entity_type, MemoryEntityType)
        canonical_key = _normalize_entity_key(normalized_name, normalized_entity_type)
        requested_aliases = sorted({alias.strip() for alias in aliases or [] if alias.strip()})

        async with get_session() as db:
            result = await db.execute(
                select(MemoryEntity)
                .where(MemoryEntity.canonical_key == canonical_key)
            )
            entity = result.scalars().first()
            if entity is None and requested_aliases:
                alias_candidates = (
                    await db.execute(
                        select(MemoryEntity).where(MemoryEntity.entity_type == normalized_entity_type)
                    )
                ).scalars().all()
                requested_alias_keys = {_normalize_entity_key(alias, normalized_entity_type) for alias in requested_aliases}
                for candidate in alias_candidates:
                    candidate_keys = {_normalize_entity_key(candidate.canonical_name, normalized_entity_type)}
                    candidate_keys.update(
                        _normalize_entity_key(alias, normalized_entity_type)
                        for alias in json.loads(candidate.aliases_json or "[]")
                    )
                    if requested_alias_keys & candidate_keys or canonical_key in candidate_keys:
                        entity = candidate
                        break
            if entity is not None:
                updated_aliases = sorted(
                    {
                        alias.strip()
                        for alias in requested_aliases + json.loads(entity.aliases_json or "[]")
                        if alias.strip()
                    }
                )
                if updated_aliases != json.loads(entity.aliases_json or "[]"):
                    entity.aliases_json = json.dumps(updated_aliases)
                    entity.updated_at = _now()
                    db.add(entity)
                await db.flush()
                db.expunge(entity)
                return entity

            entity = MemoryEntity(
                canonical_key=canonical_key,
                canonical_name=normalized_name,
                entity_type=normalized_entity_type,
                aliases_json=json.dumps(requested_aliases),
            )
            db.add(entity)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                result = await db.execute(
                    select(MemoryEntity).where(MemoryEntity.canonical_key == canonical_key)
                )
                entity = result.scalars().one()
            db.expunge(entity)
            return entity

    async def create_m5_memory_in_session(
        self,
        db,
        *,
        content: str,
        kind: MemoryKind,
        source_session_id: str,
        scope_key: str,
        metadata_json: str,
        confidence: float,
        corrects_memory_id: str | None = None,
        proposal_id: str = "",
    ) -> Memory:
        """Write one M5 memory through the canonical repository transaction.

        The caller owns ``BEGIN IMMEDIATE``.  This is the transaction-aware
        counterpart to the public correction writer; it preserves the same
        Memory/MemorySource/edge/tombstone authority without opening a second
        session or creating another memory store.
        """

        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("content must be non-empty")
        normalized_kind = _coerce_enum(kind, MemoryKind)
        if normalized_kind not in {MemoryKind.fact, MemoryKind.pattern}:
            raise ValueError("M5 memories must be fact or pattern")
        existing = (
            await db.execute(
                select(Memory).where(
                    Memory.kind == normalized_kind,
                    Memory.scope_key == scope_key,
                )
            )
        ).scalars().first()
        if existing is not None:
            existing_provenance = {}
            try:
                existing_provenance = json.loads(existing.metadata_json or "{}").get(
                    "work_board_provenance", {}
                )
            except (TypeError, ValueError, AttributeError):
                existing_provenance = {}
            if (
                existing.source_session_id != source_session_id
                or existing_provenance.get("proposal_id") != proposal_id
                or existing.content != normalized_content
                or _canonical_memory_deletion_marker(existing) is not None
            ):
                raise ValueError("canonical_write_conflict")
            return existing

        memory = Memory(
            content=normalized_content,
            category=MemoryCategory.fact if normalized_kind is MemoryKind.fact else MemoryCategory.pattern,
            kind=normalized_kind,
            summary=None,
            confidence=max(0.0, min(1.0, float(confidence))),
            importance=0.5,
            reinforcement=1.0,
            status=MemoryStatus.active,
            source_session_id=source_session_id,
            scope_key=scope_key,
            metadata_json=metadata_json,
            last_confirmed_at=_now(),
        )
        db.add(memory)
        await db.flush()
        db.add(
            MemorySource(
                memory_id=memory.id,
                source_type="work_board_m5",
                source_session_id=source_session_id,
                source_message_id=None,
                snippet=None,
            )
        )
        if corrects_memory_id:
            target = (
                await db.execute(select(Memory).where(Memory.id == corrects_memory_id))
            ).scalars().first()
            if target is None or target.source_session_id != source_session_id:
                raise PermissionError("correction_target_owner_mismatch")
            target_tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(MemoryTombstone.memory_id == corrects_memory_id)
                )
            ).scalars().first()
            if target_tombstone is not None or _canonical_memory_deletion_marker(target) is not None:
                raise ValueError("correction_target_deleted")
            target.status = MemoryStatus.superseded
            target.updated_at = _now()
            db.add(target)
            edge_metadata = json.dumps({"proposal_id": proposal_id}, sort_keys=True)
            db.add(
                MemoryEdge(
                    from_memory_id=memory.id,
                    to_memory_id=target.id,
                    edge_type=MemoryEdgeType.supersedes,
                    metadata_json=edge_metadata,
                )
            )
            db.add(
                MemoryEdge(
                    from_memory_id=memory.id,
                    to_memory_id=target.id,
                    edge_type=MemoryEdgeType.contradicts,
                    metadata_json=edge_metadata,
                )
            )
        await db.flush()
        return memory

    async def rollback_m5_memory_in_session(
        self,
        db,
        *,
        memory_id: str,
        expected_content_digest: str,
        expected_proposal_id: str,
        rollback_reason: str,
    ) -> Memory:
        """Conditionally suppress an M5 memory inside its caller transaction."""

        memory = (
            await db.execute(select(Memory).where(Memory.id == memory_id))
        ).scalars().first()
        if memory is None:
            raise ValueError("accepted_memory_not_found")
        if memory.source_session_id is None or _canonical_memory_deletion_marker(memory) is not None:
            raise ValueError("memory_deleted_or_export_redacted")
        try:
            metadata = json.loads(memory.metadata_json or "{}")
        except (TypeError, ValueError):
            metadata = {}
        provenance = metadata.get("work_board_provenance") if isinstance(metadata, dict) else {}
        if not isinstance(provenance, dict) or provenance.get("proposal_id") != expected_proposal_id:
            raise ValueError("memory_provenance_mismatch")
        actual_digest = hashlib.sha256(memory.content.encode("utf-8")).hexdigest()
        if actual_digest != expected_content_digest or memory.status is not MemoryStatus.active:
            raise ValueError("memory_changed_before_rollback")
        correction_binding = _m5_correction_binding(provenance)
        if correction_binding is None:
            raise ValueError("correction_target_binding_invalid")
        try:
            _m5_selection_binding_key_id()
        except CapabilityJournalError:
            # A rollback of a memory without a correction target is still a
            # canonical operator action when the selection-MAC key is down.
            # A correction target has a second state transition to protect;
            # without the binding key it must remain fail-closed.
            if correction_binding["corrects_memory_id"] is not None:
                raise ValueError("correction_target_binding_mismatch")
        else:
            if not _m5_selection_binding_matches(
                provenance,
                proposal_id=expected_proposal_id,
                accepted_content_digest=actual_digest,
                decision_effect=provenance.get("decision_effect"),
                memory_scope=provenance.get("memory_scope"),
                source_binding=provenance.get("verified_source_binding"),
                corrects_memory_id=correction_binding["corrects_memory_id"],
                recovered_from_proposal_id=provenance.get("recovered_from_proposal_id"),
            ):
                raise ValueError("correction_target_binding_mismatch")
        normalized_rollback_reason = str(rollback_reason or "").strip()
        if not normalized_rollback_reason or len(normalized_rollback_reason) > 500:
            raise ValueError("rollback_reason_invalid")
        corrected_memory_id = str(provenance.get("corrects_memory_id") or "").strip()
        if corrected_memory_id:
            target = (
                await db.execute(select(Memory).where(Memory.id == corrected_memory_id))
            ).scalars().first()
            if target is None or target.source_session_id != memory.source_session_id:
                raise ValueError("correction_target_unavailable")
            target_tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(MemoryTombstone.memory_id == corrected_memory_id)
                )
            ).scalars().first()
            if target_tombstone is not None or _canonical_memory_deletion_marker(target) is not None:
                raise ValueError("correction_target_deleted")
            try:
                previous_status = MemoryStatus(
                    str(provenance.get("corrected_memory_previous_status") or "")
                )
            except ValueError as exc:
                raise ValueError("correction_target_rollback_binding_invalid") from exc
            if previous_status is not MemoryStatus.active or target.status is not MemoryStatus.superseded:
                raise ValueError("correction_target_changed_before_rollback")
            from src.memory.m5 import m5_text_digest

            try:
                target_digest = m5_text_digest(target.content)
            except (TypeError, ValueError) as exc:
                raise ValueError("correction_target_changed_before_rollback") from exc
            if target_digest != correction_binding["corrected_memory_content_digest"]:
                raise ValueError("correction_target_changed_before_rollback")
            later_supersession = (
                await db.execute(
                    select(MemoryEdge).where(
                        MemoryEdge.to_memory_id == corrected_memory_id,
                        MemoryEdge.edge_type == MemoryEdgeType.supersedes,
                        MemoryEdge.from_memory_id != memory_id,
                    ).limit(1)
                )
            ).scalars().first()
            if later_supersession is not None:
                raise ValueError("correction_target_has_later_supersession")
            target.status = previous_status
            target.updated_at = _now()
            db.add(target)
        owner_principal_id = str(provenance.get("owner_principal_id") or "").strip()
        owner_session_id = str(
            provenance.get("owner_session_id") or memory.source_session_id or ""
        ).strip()
        if (
            not owner_principal_id
            or not owner_session_id
            or owner_session_id != str(memory.source_session_id or "").strip()
        ):
            raise ValueError("memory_provenance_mismatch")
        rollback_at = _now()
        rollback_at_value = _recovery_timestamp(rollback_at)
        if rollback_at_value is None:  # pragma: no cover - _now() is always aware.
            raise ValueError("rollback_timestamp_unavailable")
        provenance["lifecycle_state"] = "rolled_back"
        provenance["lifecycle_at"] = rollback_at_value
        provenance["lifecycle_reason"] = normalized_rollback_reason
        try:
            provenance["selection_binding_key_id"] = _m5_selection_binding_key_id()
            provenance["selection_binding_mac"] = _m5_selection_binding_mac(
                proposal_id=str(provenance.get("proposal_id") or ""),
                accepted_content_digest=actual_digest,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                source_context_digest=str(provenance.get("source_context_digest") or ""),
                source_binding=provenance.get("verified_source_binding"),
                decision_effect=provenance.get("decision_effect"),
                memory_scope=provenance.get("memory_scope"),
                corrects_memory_id=correction_binding["corrects_memory_id"],
                corrected_memory_previous_status=correction_binding[
                    "corrected_memory_previous_status"
                ],
                corrected_memory_content_digest=correction_binding[
                    "corrected_memory_content_digest"
                ],
                recovered_from_proposal_id=provenance.get("recovered_from_proposal_id"),
                lifecycle_state="rolled_back",
                lifecycle_at=rollback_at_value,
                lifecycle_reason=normalized_rollback_reason,
            )
        except (CapabilityJournalError, TypeError, ValueError):
            # A rolled-back memory must no longer carry an active acceptance
            # signature, even if signing is unavailable at rollback time.
            provenance["selection_binding_key_id"] = ""
            provenance["selection_binding_mac"] = ""
        metadata["work_board_provenance"] = provenance
        # Rollback itself remains canonical when the existing execution MAC
        # key is unavailable.  In that case keep only the historical marker;
        # archive restore treats it as unverifiable and quarantines the
        # memory/proposal rather than treating it as an activation authority.
        rollback_marker = {
            "proposal_id": expected_proposal_id,
            "at": rollback_at_value,
            "rollback_reason": normalized_rollback_reason,
        }
        try:
            signed_rollback_marker = {
                "version": _M5_ROLLBACK_BINDING_SCHEMA_VERSION,
                "memory_id": memory.id,
                "proposal_id": expected_proposal_id,
                "accepted_content_digest": actual_digest,
                "owner_principal_id": owner_principal_id,
                "owner_session_id": owner_session_id,
                "rollback_at": rollback_at_value,
                "rollback_reason": normalized_rollback_reason,
                # Keep the historical alias for content-free archive readers;
                # the canonical timestamp above is the field covered by the
                # MAC.
                "at": rollback_at_value,
                "key_id": _m5_rollback_binding_key_id(),
            }
            signed_rollback_marker["binding_mac"] = _m5_rollback_binding_mac(
                memory_id=memory.id,
                proposal_id=expected_proposal_id,
                accepted_content_digest=actual_digest,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                rollback_at=rollback_at_value,
                rollback_reason=normalized_rollback_reason,
            )
        except (CapabilityJournalError, TypeError, ValueError):
            signed_rollback_marker = None
        if signed_rollback_marker is not None:
            rollback_marker = signed_rollback_marker
        metadata["work_board_rollback"] = rollback_marker
        memory.status = MemoryStatus.archived
        memory.metadata_json = json.dumps(metadata, sort_keys=True, ensure_ascii=False)
        memory.updated_at = rollback_at
        db.add(memory)
        await db.flush()
        return memory

    async def list_m5_accepted_memory_candidates(
        self,
        db,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str,
        goal_revision: int,
        source_context_digest: str,
        limit: int = 3,
    ) -> list[tuple[MemoryProposal, Memory]]:
        """Resolve active M5 memory only through canonical repository rules.

        Goal and board selectors share this bounded owner/session/provenance
        lookup.  Callers never search raw ``Memory`` rows or content; a
        proposal is eligible only when its accepted memory, digest, source
        session, tombstone state, and reserved provenance all agree.
        """

        rows = list(
            (
                await db.execute(
                    select(MemoryProposal)
                    .where(
                        MemoryProposal.owner_principal_id == owner_principal_id,
                        MemoryProposal.owner_session_id == owner_session_id,
                        MemoryProposal.goal_id == goal_id,
                        MemoryProposal.goal_revision == int(goal_revision),
                        MemoryProposal.source_context_digest == source_context_digest,
                        MemoryProposal.status == MemoryProposalStatus.accepted,
                    )
                    .order_by(MemoryProposal.proposal_id.asc())
                    .limit(max(1, min(int(limit), 3)))
                )
            ).scalars().all()
        )
        eligible: list[tuple[MemoryProposal, Memory]] = []
        for proposal in rows:
            if proposal.privacy_state is MemoryProposalPrivacyState.redacted:
                continue

            def block_proposal(reason_code: str) -> None:
                proposal.status = MemoryProposalStatus.blocked
                proposal.reason_code = reason_code
                proposal.recovery_action = (
                    "verify_source_and_reaccept"
                    if reason_code == "accepted_memory_binding_unverifiable"
                    else "request_verified_proposal_again"
                )
                proposal.revision = int(proposal.revision or 0) + 1
                proposal.updated_at = _now()
                db.add(proposal)

            def quarantine_memory(memory_row: Memory) -> None:
                # Keep an unexpected active M5 record out of ordinary
                # canonical-memory retrieval while the operator reviews a
                # fresh source-bound proposal. Never change another session's
                # memory or override an existing deletion marker.
                if (
                    memory_row.source_session_id == owner_session_id
                    and memory_row.status is MemoryStatus.active
                    and _canonical_memory_deletion_marker(memory_row) is None
                ):
                    memory_row.status = MemoryStatus.archived
                    memory_row.updated_at = _now()
                    db.add(memory_row)

            memory_id = str(proposal.accepted_memory_id or "").strip()
            expected_digest = str(proposal.accepted_memory_content_digest or "").strip().lower()
            if not memory_id or not _M5_RECOVERY_DIGEST.fullmatch(expected_digest):
                block_proposal("accepted_memory_binding_unverifiable")
                continue
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            if memory is None:
                tombstone = (
                    await db.execute(
                        select(MemoryTombstone).where(MemoryTombstone.memory_id == memory_id)
                    )
                ).scalars().first()
                if tombstone is not None:
                    continue
                block_proposal("accepted_memory_binding_unverifiable")
                continue
            if _canonical_memory_deletion_marker(memory) is not None:
                continue
            tombstone = (
                await db.execute(select(MemoryTombstone).where(MemoryTombstone.memory_id == memory.id))
            ).scalars().first()
            if tombstone is not None:
                continue
            if memory.status is MemoryStatus.superseded:
                # A reviewed correction has replaced this accepted version.
                continue
            metadata: dict[str, Any] = {}
            provenance: Any = {}
            try:
                parsed_metadata = json.loads(memory.metadata_json or "{}")
                if isinstance(parsed_metadata, dict):
                    metadata = parsed_metadata
                    provenance = parsed_metadata.get("work_board_provenance", {})
            except (TypeError, ValueError, AttributeError):
                pass
            if memory.status is MemoryStatus.archived:
                operator_control = metadata.get("operator_control")
                if not isinstance(operator_control, dict):
                    operator_control = {}
                # Respect explicit canonical controls; never turn an operator
                # forget, rejection, or rollback into a recovery proposal.
                if (
                    metadata.get("archived_reason") == "operator_forget"
                    or operator_control.get("review_outcome") == "rejected"
                    or (
                        isinstance(provenance, dict)
                        and provenance.get("lifecycle_state") == "rolled_back"
                    )
                ):
                    continue
                block_proposal("accepted_memory_binding_mismatch")
                continue
            if memory.status is not MemoryStatus.active:
                block_proposal("accepted_memory_binding_unverifiable")
                continue
            if memory.source_session_id != owner_session_id:
                block_proposal("accepted_memory_binding_mismatch")
                continue
            if hashlib.sha256(memory.content.encode("utf-8")).hexdigest() != expected_digest:
                block_proposal("accepted_memory_binding_mismatch")
                quarantine_memory(memory)
                continue
            if not isinstance(provenance, dict):
                block_proposal("accepted_memory_binding_mismatch")
                quarantine_memory(memory)
                continue
            if (
                provenance.get("proposal_id") != proposal.proposal_id
                or provenance.get("owner_principal_id") != owner_principal_id
                or provenance.get("owner_session_id") != owner_session_id
                or provenance.get("source_context_digest") != source_context_digest
            ):
                block_proposal("accepted_memory_binding_mismatch")
                quarantine_memory(memory)
                continue
            try:
                proposal_scope = json.loads(proposal.memory_scope_json or "{}")
            except (TypeError, ValueError):
                proposal_scope = None
            if not _m5_selection_binding_matches(
                provenance,
                proposal_id=proposal.proposal_id,
                accepted_content_digest=expected_digest,
                decision_effect=proposal.decision_effect,
                memory_scope=proposal_scope,
                source_binding=proposal,
                recovered_from_proposal_id=proposal.recovered_from_proposal_id,
            ):
                failure_reason = _m5_selection_binding_failure_reason(
                    provenance,
                    proposal_id=proposal.proposal_id,
                    accepted_content_digest=expected_digest,
                    decision_effect=proposal.decision_effect,
                    memory_scope=proposal_scope,
                    source_binding=proposal,
                    recovered_from_proposal_id=proposal.recovered_from_proposal_id,
                )
                proposal.status = MemoryProposalStatus.blocked
                proposal.reason_code = (
                    "accepted_memory_binding_unverifiable"
                    if failure_reason == "unverifiable"
                    else "accepted_memory_binding_mismatch"
                )
                proposal.recovery_action = (
                    "verify_source_and_reaccept"
                    if failure_reason == "unverifiable"
                    else "request_verified_proposal_again"
                )
                proposal.revision = int(proposal.revision or 0) + 1
                proposal.updated_at = _now()
                db.add(proposal)
                if failure_reason == "mismatch":
                    quarantine_memory(memory)
                continue
            has_source = (
                await db.execute(
                    select(MemorySource).where(
                        MemorySource.memory_id == memory.id,
                        MemorySource.source_type == "work_board_m5",
                        MemorySource.source_session_id == owner_session_id,
                        MemorySource.source_message_id.is_(None),
                    )
                )
            ).scalars().first()
            if has_source is None:
                block_proposal("accepted_memory_binding_mismatch")
                quarantine_memory(memory)
                continue
            if proposal.recovered_from_proposal_id:
                recovery_parent = (
                    await db.execute(
                        select(MemoryProposal).where(
                            MemoryProposal.proposal_id
                            == proposal.recovered_from_proposal_id,
                            MemoryProposal.owner_principal_id == owner_principal_id,
                            MemoryProposal.owner_session_id == owner_session_id,
                        )
                    )
                ).scalars().first()
                from src.memory.m5 import _m5_authenticated_recovery_link

                if recovery_parent is None or not _m5_authenticated_recovery_link(
                    recovery_parent,
                    proposal,
                    memory,
                ):
                    block_proposal("recovery_parent_binding_mismatch")
                    quarantine_memory(memory)
                    continue
            eligible.append((proposal, memory))
        return eligible

    async def create_memory(
        self,
        *,
        content: str,
        category: MemoryCategory | str = MemoryCategory.fact,
        kind: MemoryKind | str = MemoryKind.fact,
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        source_type: str = "session",
        source_snippet: str | None = None,
        summary: str | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
        reinforcement: float = 1.0,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        embedding_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: MemoryStatus | str = MemoryStatus.active,
        last_confirmed_at: datetime | None = None,
        additional_sources: list[dict[str, str | None]] | None = None,
        supersedes_memory_id: str | None = None,
        supersedes_metadata: dict[str, Any] | None = None,
    ) -> MemoryWriteResult:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("content must be non-empty")
        normalized_category = _coerce_enum(category, MemoryCategory)
        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_embedding_id = (
            embedding_id.strip()
            if isinstance(embedding_id, str) and embedding_id.strip()
            else None
        )
        normalized_source_type = source_type.strip() or "session"
        normalized_source_snippet = (
            " ".join(source_snippet.strip().split())[:240]
            if isinstance(source_snippet, str) and source_snippet.strip()
            else None
        )
        source_rows: list[dict[str, str | None]] = []
        if source_session_id or source_message_id:
            source_rows.append(
                {
                    "source_type": normalized_source_type,
                    "source_session_id": source_session_id,
                    "source_message_id": source_message_id,
                    "snippet": normalized_source_snippet,
                }
            )
        source_rows.extend(additional_sources or [])

        normalized_supersedes_memory_id = (
            str(supersedes_memory_id or "").strip() or None
        )
        async with get_session() as db:
            superseded_memory: Memory | None = None
            superseded_expected_metadata: str | None = None
            superseded_expected_status: MemoryStatus | None = None
            superseded_expected_updated_at: datetime | None = None
            if normalized_supersedes_memory_id is not None:
                await _begin_canonical_write(db)
                superseded_memory = (
                    await db.execute(
                        select(Memory).where(
                            Memory.id == normalized_supersedes_memory_id
                        )
                    )
                ).scalars().first()
                if superseded_memory is None:
                    raise ValueError(
                        f"Unknown memory id: {normalized_supersedes_memory_id}"
                    )
                tombstone = (
                    await db.execute(
                        select(MemoryTombstone).where(
                            MemoryTombstone.memory_id == normalized_supersedes_memory_id
                        )
                    )
                ).scalars().first()
                if tombstone is not None or _canonical_memory_deletion_marker(
                    superseded_memory
                ) is not None:
                    raise ValueError(
                        "cannot correct canonical memory after operator delete/export redaction"
                    )
                superseded_expected_metadata = superseded_memory.metadata_json
                superseded_expected_status = _coerce_enum(
                    superseded_memory.status, MemoryStatus
                )
                superseded_expected_updated_at = superseded_memory.updated_at

            metadata_map = dict(metadata or {})
            memory = Memory(
                content=normalized_content,
                category=normalized_category,
                kind=normalized_kind,
                summary=summary,
                confidence=confidence,
                importance=importance,
                reinforcement=reinforcement,
                status=normalized_status,
                subject_entity_id=subject_entity_id,
                project_entity_id=project_entity_id,
                source_session_id=source_session_id,
                embedding_id=normalized_embedding_id,
                metadata_json=json.dumps(metadata_map, sort_keys=True),
                last_confirmed_at=last_confirmed_at,
                updated_at=_now(),
            )
            db.add(memory)
            await db.flush()

            message_source_count = 0
            session_source_created = False
            seen_source_keys: set[tuple[str, str | None, str | None]] = set()
            for source_row in source_rows:
                row_source_type = str(source_row.get("source_type") or "session").strip() or "session"
                row_session_id = source_row.get("source_session_id")
                row_message_id = source_row.get("source_message_id")
                if not row_session_id and not row_message_id:
                    continue
                source_key = (
                    "message",
                    row_message_id,
                    None,
                ) if row_message_id is not None else (
                    "session",
                    row_session_id,
                    row_source_type,
                )
                if source_key in seen_source_keys:
                    continue
                seen_source_keys.add(source_key)
                db.add(
                    MemorySource(
                        memory_id=memory.id,
                        source_type=row_source_type,
                        source_session_id=row_session_id,
                        source_message_id=row_message_id,
                        snippet=(
                            self._normalize_source_snippet(source_row.get("snippet"))
                            or (summary or normalized_content)[:240]
                        ),
                    )
                )
                if row_message_id is not None:
                    message_source_count += 1
                elif row_session_id is not None:
                    session_source_created = True
            if source_rows:
                await db.flush()

            if superseded_memory is not None:
                try:
                    superseded_metadata_map = json.loads(
                        superseded_memory.metadata_json or "{}"
                    )
                except (TypeError, json.JSONDecodeError):
                    superseded_metadata_map = {}
                if not isinstance(superseded_metadata_map, dict):
                    superseded_metadata_map = {}
                superseded_metadata_map.update(supersedes_metadata or {})
                superseded_metadata_map.setdefault(
                    "superseded_by_memory_id", memory.id
                )
                expected_metadata_guard = (
                    Memory.metadata_json.is_(None)
                    if superseded_expected_metadata is None
                    else Memory.metadata_json == superseded_expected_metadata
                )
                superseded_updated = await db.execute(
                    update(Memory)
                    .where(
                        Memory.id == normalized_supersedes_memory_id,
                        Memory.status == superseded_expected_status,
                        Memory.updated_at == superseded_expected_updated_at,
                        expected_metadata_guard,
                        _canonical_memory_without_tombstone_clause(),
                    )
                    .values(
                        status=MemoryStatus.superseded,
                        metadata_json=json.dumps(
                            superseded_metadata_map, sort_keys=True
                        ),
                        updated_at=_now(),
                    )
                )
                if superseded_updated.rowcount != 1:
                    raise ValueError(
                        "memory changed before correction; canonical deletion or another memory control won"
                    )

            db.expunge(memory)
            return MemoryWriteResult(
                memory_id=memory.id,
                subject_entity_id=subject_entity_id,
                project_entity_id=project_entity_id,
                message_source_count=message_source_count,
                session_source_created=session_source_created,
            )

    async def add_memory_source(
        self,
        *,
        memory_id: str,
        source_type: str = "session",
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        snippet: str | None = None,
    ) -> MemorySourceWriteResult:
        normalized_source_type = source_type.strip() or "session"
        normalized_snippet = (
            " ".join(snippet.strip().split())[:240]
            if isinstance(snippet, str) and snippet.strip()
            else None
        )
        async with get_session() as db:
            await _begin_canonical_write(db)
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {memory_id}")
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None or _canonical_memory_deletion_marker(memory) is not None:
                raise ValueError(
                    "cannot add provenance to canonical memory after operator delete/export redaction"
                )
            stmt = select(MemorySource).where(MemorySource.memory_id == memory_id)
            if source_message_id:
                stmt = stmt.where(MemorySource.source_message_id == source_message_id)
            else:
                stmt = stmt.where(MemorySource.source_message_id.is_(None))
                if source_session_id is not None:
                    stmt = stmt.where(MemorySource.source_session_id == source_session_id)
                stmt = stmt.where(MemorySource.source_type == normalized_source_type)
            existing = (await db.execute(stmt)).scalars().first()
            if existing is not None:
                db.expunge(existing)
                return MemorySourceWriteResult(source_id=existing.id, created=False)

            # Touch the canonical row under the tombstone predicate before
            # inserting provenance.  The write lock now serializes this
            # source insertion with delete/export across processes.
            guarded_memory_update = await db.execute(
                update(Memory)
                .where(
                    Memory.id == memory_id,
                    _canonical_memory_without_tombstone_clause(),
                )
                .values(updated_at=_now())
            )
            if guarded_memory_update.rowcount != 1:
                raise ValueError(
                    "cannot add provenance to canonical memory after operator delete/export redaction"
                )

            source = MemorySource(
                memory_id=memory_id,
                source_type=normalized_source_type,
                source_session_id=source_session_id,
                source_message_id=source_message_id,
                snippet=normalized_snippet,
            )
            db.add(source)
            await db.flush()
            db.expunge(source)
            return MemorySourceWriteResult(source_id=source.id, created=True)

    async def list_sources(
        self,
        *,
        memory_id: str,
    ) -> list[MemorySource]:
        async with get_session() as db:
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == memory_id
                    )
                )
            ).scalars().first()
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            redact_sources = tombstone is not None or (
                memory is not None and _canonical_memory_deletion_marker(memory) is not None
            )
            result = await db.execute(
                select(MemorySource)
                .where(MemorySource.memory_id == memory_id)
                .order_by(col(MemorySource.created_at).asc())
            )
            sources = result.scalars().all()
            for source in sources:
                if redact_sources:
                    source.snippet = None
                db.expunge(source)
            return list(sources)

    async def find_merge_candidate(
        self,
        *,
        kind: MemoryKind | str,
        summary: str | None,
        content: str,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> Memory | None:
        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_summary = self._normalize_memory_text(summary)
        normalized_content = self._normalize_memory_text(content)
        if not normalized_summary and not normalized_content:
            return None

        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.kind == normalized_kind)
                .where(Memory.status == normalized_status)
                .where(_canonical_memory_without_tombstone_clause())
                .order_by(
                    col(Memory.reinforcement).desc(),
                    col(Memory.importance).desc(),
                    col(Memory.updated_at).desc(),
                    col(Memory.created_at).desc(),
                )
            )
            if subject_entity_id is not None:
                stmt = stmt.where(
                    or_(
                        Memory.subject_entity_id == subject_entity_id,
                        Memory.subject_entity_id.is_(None),
                    )
                )
            else:
                stmt = stmt.where(Memory.subject_entity_id.is_(None))
            if project_entity_id is not None:
                stmt = stmt.where(
                    or_(
                        Memory.project_entity_id == project_entity_id,
                        Memory.project_entity_id.is_(None),
                    )
                )
            else:
                stmt = stmt.where(Memory.project_entity_id.is_(None))

            candidates = (await db.execute(stmt)).scalars().all()
            for memory in candidates:
                if _canonical_memory_deletion_marker(memory) is not None:
                    continue
                candidate_summary = self._normalize_memory_text(memory.summary)
                candidate_content = self._normalize_memory_text(memory.content)
                if normalized_summary and candidate_summary == normalized_summary:
                    db.expunge(memory)
                    return memory
                if normalized_content and candidate_content == normalized_content:
                    db.expunge(memory)
                    return memory
            return None

    async def merge_memory(
        self,
        memory_id: str,
        *,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        reinforcement_delta: float = 0.25,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        embedding_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        last_confirmed_at: datetime | None = None,
        message_sources: list[dict[str, str | None]] | None = None,
        session_source: dict[str, str | None] | None = None,
    ) -> MemoryWriteResult:
        def _normalize_timestamp(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        async with get_session() as db:
            await _begin_canonical_write(db)
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {memory_id}")
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None or _canonical_memory_deletion_marker(memory) is not None:
                raise ValueError(
                    "cannot merge canonical memory after operator delete/export redaction"
                )

            created_message_source_count = 0
            session_source_created = False
            expected_metadata = memory.metadata_json
            expected_status = _coerce_enum(memory.status, MemoryStatus)
            expected_updated_at = _normalize_timestamp(memory.updated_at)
            expected_metadata_guard = (
                Memory.metadata_json.is_(None)
                if expected_metadata is None
                else Memory.metadata_json == expected_metadata
            )
            existing_metadata = json.loads(memory.metadata_json or "{}")
            metadata_changed = False

            if summary and not memory.summary:
                memory.summary = summary
            if confidence is not None:
                memory.confidence = max(memory.confidence, confidence)
            if importance is not None:
                memory.importance = max(memory.importance, importance)
            memory.reinforcement = max(0.0, memory.reinforcement + reinforcement_delta)
            if memory.subject_entity_id is None and subject_entity_id is not None:
                memory.subject_entity_id = subject_entity_id
            if memory.project_entity_id is None and project_entity_id is not None:
                memory.project_entity_id = project_entity_id
            if memory.embedding_id is None and embedding_id:
                memory.embedding_id = embedding_id
            if last_confirmed_at is not None:
                normalized_existing = _normalize_timestamp(memory.last_confirmed_at)
                normalized_candidate = _normalize_timestamp(last_confirmed_at)
                if (
                    normalized_candidate is not None
                    and (
                        normalized_existing is None
                        or normalized_candidate > normalized_existing
                    )
                ):
                    memory.last_confirmed_at = normalized_candidate
                    for key in (
                        "decay_step",
                        "decay_age_days",
                        "archived_reason",
                        "archived_at",
                    ):
                        if key in existing_metadata:
                            existing_metadata.pop(key, None)
                            metadata_changed = True
            if metadata:
                existing_metadata.update(metadata)
                metadata_changed = True
            if metadata_changed:
                memory.metadata_json = json.dumps(existing_metadata, sort_keys=True)
            memory.updated_at = _now()
            updated_at = memory.updated_at
            db.expunge(memory)
            updated_memory = await db.execute(
                update(Memory)
                .where(
                    Memory.id == memory_id,
                    Memory.updated_at == expected_updated_at,
                    Memory.status == expected_status,
                    expected_metadata_guard,
                    _canonical_memory_without_tombstone_clause(),
                )
                .values(
                    summary=memory.summary,
                    confidence=memory.confidence,
                    importance=memory.importance,
                    reinforcement=memory.reinforcement,
                    subject_entity_id=memory.subject_entity_id,
                    project_entity_id=memory.project_entity_id,
                    embedding_id=memory.embedding_id,
                    last_confirmed_at=memory.last_confirmed_at,
                    metadata_json=memory.metadata_json,
                    updated_at=updated_at,
                )
            )
            if updated_memory.rowcount != 1:
                raise ValueError(
                    "memory changed before merge; canonical deletion or another memory control won"
                )
            for source in message_sources or []:
                source_message_id = source.get("source_message_id")
                if not source_message_id:
                    continue
                existing = (
                    await db.execute(
                        select(MemorySource)
                        .where(MemorySource.memory_id == memory_id)
                        .where(MemorySource.source_message_id == source_message_id)
                    )
                ).scalars().first()
                if existing is not None:
                    continue
                db.add(
                    MemorySource(
                        memory_id=memory_id,
                        source_type="message",
                        source_session_id=source.get("source_session_id"),
                        source_message_id=source_message_id,
                        snippet=self._normalize_source_snippet(source.get("snippet")),
                    )
                )
                created_message_source_count += 1

            if session_source:
                source_session_id = session_source.get("source_session_id")
                if source_session_id:
                    existing = (
                        await db.execute(
                            select(MemorySource)
                            .where(MemorySource.memory_id == memory_id)
                            .where(MemorySource.source_type == "session")
                            .where(MemorySource.source_session_id == source_session_id)
                            .where(MemorySource.source_message_id.is_(None))
                        )
                    ).scalars().first()
                    if existing is None:
                        db.add(
                            MemorySource(
                                memory_id=memory_id,
                                source_type="session",
                                source_session_id=source_session_id,
                                snippet=self._normalize_source_snippet(session_source.get("snippet")),
                            )
                        )
                        session_source_created = True

            await db.flush()
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().one()
            db.expunge(memory)
            return MemoryWriteResult(
                memory_id=memory.id,
                subject_entity_id=memory.subject_entity_id,
                project_entity_id=memory.project_entity_id,
                message_source_count=created_message_source_count,
                session_source_created=session_source_created,
            )

    async def _cas_update_scoped_memory(
        self,
        db,
        memory: Memory,
        *,
        values_builder,
    ) -> Memory | None:
        """Update an existing scoped row only when its control snapshot holds."""

        def _normalize_timestamp(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        async def _select_memory_by_id(memory_id: str) -> Memory | None:
            return (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()

        for _attempt in range(2):
            if _canonical_memory_deletion_marker(memory) is not None:
                return None
            expected_metadata = memory.metadata_json
            expected_status = _coerce_enum(memory.status, MemoryStatus)
            expected_updated_at = _normalize_timestamp(memory.updated_at)
            expected_metadata_guard = (
                Memory.metadata_json.is_(None)
                if expected_metadata is None
                else Memory.metadata_json == expected_metadata
            )
            result = await db.execute(
                update(Memory)
                .where(
                    Memory.id == memory.id,
                    Memory.updated_at == expected_updated_at,
                    Memory.status == expected_status,
                    expected_metadata_guard,
                    _canonical_memory_without_tombstone_clause(),
                )
                .values(**values_builder(memory))
            )
            if result.rowcount == 1:
                return memory

            # A competing control may have committed after the read. Reset
            # this transaction before reconciling the current row so a winning
            # canonical delete can never be overwritten.
            await db.rollback()
            await _begin_canonical_write(db)
            memory = await _select_memory_by_id(memory.id)
            if memory is None:
                return None
        return None

    async def sync_scoped_memory(
        self,
        *,
        kind: MemoryKind | str,
        scope: dict[str, Any],
        content: str | None,
        summary: str | None = None,
        category: MemoryCategory | str | None = None,
        confidence: float = 0.7,
        importance: float = 0.7,
        reinforcement: float = 1.0,
        metadata: dict[str, Any] | None = None,
        source_session_id: str | None = None,
        last_confirmed_at: datetime | None = None,
    ) -> MemoryWriteResult | None:
        """Create or refresh one memory for a stable kind/scope pair.

        ``None`` means either the existing empty-content archive path ran, a
        non-empty ingress was suppressed because the existing row carries a
        canonical delete/export tombstone, or a concurrent row change could
        not be reconciled safely. These outcomes are no-ops and are not
        successful write receipts.
        """

        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_category = _coerce_enum(
            category if category is not None else MemoryCategory.preference,
            MemoryCategory,
        )
        normalized_scope = {
            str(key): value
            for key, value in (scope or {}).items()
            if str(key).strip() and value is not None
        }
        if not normalized_scope:
            raise ValueError("scope must contain at least one key")

        normalized_content = content.strip() if isinstance(content, str) else ""
        normalized_summary = (
            summary.strip() if isinstance(summary, str) and summary.strip() else None
        )
        merged_metadata = dict(normalized_scope)
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        normalized_scope_key = self._scoped_memory_key(
            kind=normalized_kind,
            scope=normalized_scope,
        )

        def _normalize_timestamp(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        def _matches_scope(memory: Memory) -> bool:
            try:
                payload = json.loads(memory.metadata_json or "{}")
            except json.JSONDecodeError:
                return False
            if not isinstance(payload, dict):
                return False
            return all(payload.get(key) == value for key, value in normalized_scope.items())

        lock = self._get_scoped_memory_lock(
            kind=normalized_kind,
            scope=normalized_scope,
        )
        async with lock:
            async with get_session() as db:
                await _begin_canonical_write(db)
                memory = (
                    await db.execute(
                        select(Memory)
                        .where(Memory.kind == normalized_kind)
                        .where(Memory.scope_key == normalized_scope_key)
                        .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
                    )
                ).scalars().first()
                if memory is None:
                    candidates = (
                        await db.execute(
                            select(Memory)
                            .where(Memory.kind == normalized_kind)
                            .order_by(col(Memory.updated_at).desc(), col(Memory.created_at).desc())
                        )
                    ).scalars().all()
                    memory = next((item for item in candidates if _matches_scope(item)), None)

                if memory is not None:
                    if not normalized_content and _canonical_memory_deletion_marker(memory) is not None:
                        return None
                    tombstone = (
                        await db.execute(select(MemoryTombstone).where(MemoryTombstone.memory_id == memory.id))
                    ).scalars().first()
                    if tombstone is not None:
                        return None
                    if _canonical_memory_deletion_marker(memory) is not None:
                        return None

                if not normalized_content:
                    if memory is None:
                        return None
                    await self._cas_update_scoped_memory(
                        db,
                        memory,
                        values_builder=lambda _current: {
                            "status": MemoryStatus.archived,
                            "updated_at": _now(),
                            "scope_key": normalized_scope_key,
                            "metadata_json": json.dumps(merged_metadata, sort_keys=True),
                        },
                    )
                    return None

                if memory is None:
                    try:
                        unresolved_tombstone = (
                            await db.execute(
                                select(MemoryTombstone.id)
                                .outerjoin(Memory, Memory.id == MemoryTombstone.memory_id)
                                .where(Memory.id.is_(None))
                                .limit(1)
                            )
                        ).scalars().first()
                    except SQLAlchemyError:
                        # A missing or unavailable deletion authority must not
                        # permit a new learning/provider row to be created.
                        return None
                    if unresolved_tombstone is not None:
                        return None
                    memory = Memory(
                        content=normalized_content,
                        category=normalized_category,
                        kind=normalized_kind,
                        source_session_id=source_session_id,
                        summary=normalized_summary,
                        confidence=confidence,
                        importance=importance,
                        reinforcement=reinforcement,
                        scope_key=normalized_scope_key,
                        metadata_json=json.dumps(merged_metadata, sort_keys=True),
                        status=MemoryStatus.active,
                        last_confirmed_at=_normalize_timestamp(last_confirmed_at),
                    )
                    db.add(memory)
                    try:
                        await db.flush()
                    except IntegrityError:
                        await db.rollback()
                        await _begin_canonical_write(db)
                        memory = (
                            await db.execute(
                                select(Memory)
                                .where(Memory.kind == normalized_kind)
                                .where(Memory.scope_key == normalized_scope_key)
                            )
                        ).scalars().first()
                        if memory is None:
                            raise
                        memory = await self._cas_update_scoped_memory(
                            db,
                            memory,
                            values_builder=lambda current: {
                                "content": normalized_content,
                                "category": normalized_category,
                                "summary": normalized_summary,
                                "confidence": confidence,
                                "importance": importance,
                                "reinforcement": max(current.reinforcement, reinforcement),
                                "status": MemoryStatus.active,
                                "scope_key": normalized_scope_key,
                                "metadata_json": json.dumps(merged_metadata, sort_keys=True),
                                "updated_at": _now(),
                                **(
                                    {"source_session_id": source_session_id}
                                    if source_session_id
                                    else {}
                                ),
                                **(
                                    {
                                        "last_confirmed_at": _normalize_timestamp(last_confirmed_at)
                                    }
                                    if last_confirmed_at is not None
                                    else {}
                                ),
                            },
                        )
                        if memory is None:
                            return None
                    db.expunge(memory)
                    return MemoryWriteResult(memory_id=memory.id)

                memory = await self._cas_update_scoped_memory(
                    db,
                    memory,
                    values_builder=lambda current: {
                        "content": normalized_content,
                        "category": normalized_category,
                        "summary": normalized_summary,
                        "confidence": confidence,
                        "importance": importance,
                        "reinforcement": max(current.reinforcement, reinforcement),
                        "status": MemoryStatus.active,
                        "scope_key": normalized_scope_key,
                        "metadata_json": json.dumps(merged_metadata, sort_keys=True),
                        "updated_at": _now(),
                        **(
                            {"source_session_id": source_session_id}
                            if source_session_id
                            else {}
                        ),
                        **(
                            {"last_confirmed_at": _normalize_timestamp(last_confirmed_at)}
                            if last_confirmed_at is not None
                            else {}
                        ),
                    },
                )
                if memory is None:
                    return None
                db.expunge(memory)
                return MemoryWriteResult(
                    memory_id=memory.id,
                    subject_entity_id=memory.subject_entity_id,
                    project_entity_id=memory.project_entity_id,
                )

    async def create_episode(
        self,
        *,
        episode_type: MemoryEpisodeType | str = MemoryEpisodeType.conversation,
        summary: str,
        content: str,
        session_id: str | None = None,
        source_message_id: str | None = None,
        source_tool_name: str | None = None,
        source_role: str | None = None,
        subject_entity_id: str | None = None,
        project_entity_id: str | None = None,
        salience: float = 0.5,
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> MemoryEpisode:
        episodes = await self.create_episode_batch(
            items=[
                {
                    "episode_type": episode_type,
                    "summary": summary,
                    "content": content,
                    "session_id": session_id,
                    "source_message_id": source_message_id,
                    "source_tool_name": source_tool_name,
                    "source_role": source_role,
                    "subject_entity_id": subject_entity_id,
                    "project_entity_id": project_entity_id,
                    "salience": salience,
                    "confidence": confidence,
                    "metadata": metadata,
                    "observed_at": observed_at,
                }
            ]
        )
        return episodes[0]

    async def create_episode_batch(
        self,
        *,
        items: list[dict[str, Any]],
    ) -> list[MemoryEpisode]:
        if not items:
            return []
        normalized_items = [
            self._normalize_episode_kwargs(
                episode_type=item.get("episode_type", MemoryEpisodeType.conversation),
                summary=str(item.get("summary", "")),
                content=str(item.get("content", "")),
                session_id=item.get("session_id"),
                source_message_id=item.get("source_message_id"),
                source_tool_name=item.get("source_tool_name"),
                source_role=item.get("source_role"),
                subject_entity_id=item.get("subject_entity_id"),
                project_entity_id=item.get("project_entity_id"),
                salience=float(item.get("salience", 0.5)),
                confidence=float(item.get("confidence", 0.5)),
                metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else None,
                observed_at=item.get("observed_at"),
            )
            for item in items
        ]

        async with get_session() as db:
            episodes: list[MemoryEpisode] = []
            for episode_kwargs in normalized_items:
                episode = MemoryEpisode(**episode_kwargs)
                db.add(episode)
                episodes.append(episode)
            await db.flush()
            for episode in episodes:
                db.expunge(episode)
            return episodes

    async def find_entities_by_names(
        self,
        *,
        names: tuple[str, ...],
        entity_type: MemoryEntityType | str,
    ) -> dict[str, MemoryEntity]:
        normalized_entity_type = _coerce_enum(entity_type, MemoryEntityType)
        requested = {
            name: _normalize_entity_key(name, normalized_entity_type)
            for name in dict.fromkeys(name.strip() for name in names if name.strip())
        }
        if not requested:
            return {}

        async with get_session() as db:
            result = await db.execute(
                select(MemoryEntity).where(MemoryEntity.entity_type == normalized_entity_type)
            )
            entities = result.scalars().all()
            resolved: dict[str, MemoryEntity] = {}
            for entity in entities:
                candidate_keys = {
                    _normalize_entity_key(entity.canonical_name, normalized_entity_type)
                }
                candidate_keys.update(
                    _normalize_entity_key(alias, normalized_entity_type)
                    for alias in json.loads(entity.aliases_json or "[]")
                    if alias
                )
                for requested_name, requested_key in requested.items():
                    if requested_key not in candidate_keys or requested_name in resolved:
                        continue
                    resolved[requested_name] = entity
            unresolved_names = [name for name in requested if name not in resolved]
            if unresolved_names and normalized_entity_type == MemoryEntityType.project:
                for requested_name in unresolved_names:
                    matches: list[MemoryEntity] = []
                    for entity in entities:
                        candidate_names = [entity.canonical_name]
                        candidate_names.extend(
                            alias for alias in json.loads(entity.aliases_json or "[]") if alias
                        )
                        if any(
                            _name_contains_requested_tokens(candidate_name, requested_name)
                            for candidate_name in candidate_names
                        ):
                            matches.append(entity)
                    unique_matches = {entity.id: entity for entity in matches}
                    if len(unique_matches) == 1:
                        resolved[requested_name] = next(iter(unique_matches.values()))
            for entity in {id(entity): entity for entity in resolved.values()}.values():
                db.expunge(entity)
            return resolved

    async def list_memories(
        self,
        *,
        kind: MemoryKind | str | None = None,
        limit: int = 20,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> list[Memory]:
        normalized_status = _coerce_enum(status, MemoryStatus)
        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.status == normalized_status)
                .order_by(col(Memory.importance).desc(), col(Memory.created_at).desc())
                .limit(limit)
            )
            stmt = stmt.where(_canonical_memory_without_tombstone_clause())
            if kind:
                stmt = stmt.where(Memory.kind == _coerce_enum(kind, MemoryKind))
            result = await db.execute(stmt)
            memories = [
                memory
                for memory in result.scalars().all()
                if not (
                    _canonical_memory_deletion_marker(memory) is not None
                )
            ]
            for memory in memories:
                db.expunge(memory)
            return list(memories)

    async def get_memory(
        self,
        memory_id: str,
        *,
        include_deleted: bool = False,
    ) -> Memory | None:
        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            return None
        async with get_session() as db:
            stmt = select(Memory).where(Memory.id == normalized_memory_id)
            if not include_deleted:
                stmt = stmt.where(_canonical_memory_without_tombstone_clause())
            memory = (
                await db.execute(stmt)
            ).scalars().first()
            if (
                memory is not None
                and not include_deleted
                and _canonical_memory_deletion_marker(memory) is not None
            ):
                return None
            if memory is not None:
                db.expunge(memory)
            return memory

    async def get_memory_tombstone(self, memory_id: str) -> MemoryTombstone | None:
        """Read the local canonical deletion authority for one memory."""

        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            return None
        async with get_session() as db:
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == normalized_memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None:
                db.expunge(tombstone)
            return tombstone

    async def get_memory_tombstone_revision(self, *, owner_session_id: str | None = None) -> str:
        """Read a content-free revision of the canonical tombstone ledger.

        Recovery callers must provide their authenticated owner.  The optional
        unscoped form remains for internal global snapshot/index maintenance.
        """

        async with get_session() as db:
            return await _memory_tombstone_revision(db, owner_session_id=owner_session_id)

    async def export_canonical_memory_state(
        self,
        *,
        actor: str,
        owner_session_id: str,
        authenticated_session_id: str,
        source_role: str = "operator",
        limit: int = _MAX_RECOVERY_RECORDS,
    ) -> dict[str, Any]:
        """Export canonical memory rows and the content-free delete ledger.

        The export is assembled while a SQLite ``BEGIN IMMEDIATE`` transaction
        is held.  This gives the operator one tombstone revision and one row
        set, so a concurrent merge cannot produce a self-inconsistent archive.
        Deleted rows are intentionally absent; their IDs remain represented by
        content-free tombstones so an older archive cannot revive them.
        """

        normalized_actor, normalized_owner = _recovery_authority(
            actor=actor,
            owner_session_id=owner_session_id,
            authenticated_session_id=authenticated_session_id,
            source_role=source_role,
        )
        bounded_limit = min(max(int(limit), 1), _MAX_RECOVERY_RECORDS)
        reconciliation = await self.reconcile_memory_tombstones(owner_session_id=normalized_owner)
        if reconciliation.get("status") != "ready":
            return {
                "schema_version": _MEMORY_EXPORT_SCHEMA_VERSION,
                "owner_session_id": normalized_owner,
                "status": "degraded_no_learning",
                "degraded": True,
                "operator_status": "canonical_memory_recovery_degraded",
                "no_learning_reason": "canonical tombstone ledger requires repair",
                "provenance": {
                    "kind": "operator_memory_export",
                    "actor": normalized_actor,
                    "source_role": source_role,
                    "owner_session_id": normalized_owner,
                },
                "reconciliation": reconciliation,
                "memories": [],
                "tombstones": [],
                "m5_proposals": [],
                "m5_decision_receipts": [],
                "memory_ids": [],
                "tombstone_ids": [],
                "artifact_path": None,
                "artifact_sha256": None,
            }

        async with self._canonical_memory_lock:
            async with get_session() as db:
                await _begin_canonical_write(db)
                current_revision = await _memory_tombstone_revision(
                    db,
                    owner_session_id=normalized_owner,
                )
                tombstones = (
                    await db.execute(
                        select(MemoryTombstone)
                        .join(Memory, Memory.id == MemoryTombstone.memory_id)
                        .where(Memory.source_session_id == normalized_owner)
                        .order_by(
                            col(MemoryTombstone.created_at).asc(),
                            col(MemoryTombstone.id).asc(),
                        )
                    )
                ).scalars().all()
                tombstone_ids = {tombstone.memory_id for tombstone in tombstones}
                statement = (
                    select(Memory)
                    .where(~exists().where(MemoryTombstone.memory_id == Memory.id))
                    .where(Memory.source_session_id == normalized_owner)
                    .order_by(col(Memory.updated_at).asc(), col(Memory.id).asc())
                    .limit(bounded_limit)
                )
                memories = (await db.execute(statement)).scalars().all()
                memories = [
                    memory
                    for memory in memories
                    if memory.id not in tombstone_ids
                    and _canonical_memory_deletion_marker(memory) is None
                ]
                memory_ids = [memory.id for memory in memories]
                source_rows: dict[str, list[MemorySource]] = {memory_id: [] for memory_id in memory_ids}
                if memory_ids:
                    source_result = await db.execute(
                        select(MemorySource)
                        .where(MemorySource.memory_id.in_(memory_ids))
                        .order_by(col(MemorySource.created_at).asc(), col(MemorySource.id).asc())
                        .limit(_MAX_RECOVERY_SOURCE_RECORDS + 1)
                    )
                    sources = source_result.scalars().all()
                    if len(sources) > _MAX_RECOVERY_SOURCE_RECORDS:
                        raise ValueError(
                            "memory recovery source provenance exceeds the recovery limit"
                        )
                    for source in sources:
                        source_owner = str(source.source_session_id or "").strip()
                        if source_owner != normalized_owner:
                            raise PermissionError(
                                "memory recovery source provenance does not match the authenticated owner"
                            )
                        source_rows.setdefault(source.memory_id, []).append(source)
                memory_payloads = [
                    _archive_memory_payload(memory, source_rows.get(memory.id, []))
                    for memory in memories
                ]
                tombstone_payloads = [
                    {
                        "id": tombstone.id,
                        "memory_id": tombstone.memory_id,
                        "actor": tombstone.actor,
                        "reason": tombstone.reason,
                        "created_at": _recovery_timestamp(tombstone.created_at),
                    }
                    for tombstone in tombstones
                ]
                # M5 exports are content free and owner scoped.  Candidate
                # text/scope is omitted so an archive cannot become a second
                # retrieval store; canonical memory rows and tombstones remain
                # the authority on restore.
                proposals = (
                    await db.execute(
                        select(MemoryProposal)
                        .where(
                            MemoryProposal.owner_principal_id == normalized_actor,
                            MemoryProposal.owner_session_id == normalized_owner,
                        )
                        .order_by(MemoryProposal.created_at.asc(), MemoryProposal.proposal_id.asc())
                        .limit(bounded_limit)
                    )
                ).scalars().all()
                m5_proposal_payloads = [
                    _m5_proposal_archive_payload(proposal) for proposal in proposals
                ]
                receipts = (
                    await db.execute(
                        select(WorkBoardDecisionReceipt)
                        .where(
                            WorkBoardDecisionReceipt.owner_principal_id == normalized_actor,
                            WorkBoardDecisionReceipt.owner_session_id == normalized_owner,
                        )
                        .order_by(WorkBoardDecisionReceipt.created_at.asc(), WorkBoardDecisionReceipt.receipt_id.asc())
                        .limit(bounded_limit)
                    )
                ).scalars().all()
                m5_receipt_payloads = [
                    _m5_receipt_archive_payload(receipt) for receipt in receipts
                ]

        body: dict[str, Any] = {
            "schema_version": _MEMORY_EXPORT_SCHEMA_VERSION,
            "owner_session_id": normalized_owner,
            "canonical_tombstone_revision": current_revision,
            "memories": memory_payloads,
            "tombstones": tombstone_payloads,
            "m5_proposals": m5_proposal_payloads,
            "m5_decision_receipts": m5_receipt_payloads,
        }
        payload = {
            **body,
            "generated_at": _now().isoformat(),
            "status": "ready",
            "degraded": False,
            "operator_status": "canonical_memory_export_ready",
            "provenance": {
                "kind": "operator_memory_export",
                "actor": normalized_actor,
                "source_role": source_role,
                "owner_session_id": normalized_owner,
            },
            "no_learning_reason": None,
            "reconciliation": reconciliation,
            "counts": {
                "memories": len(memory_payloads),
                "tombstones": len(tombstone_payloads),
                "sources": sum(len(item.get("sources", [])) for item in memory_payloads),
                "m5_proposals": len(m5_proposal_payloads),
                "m5_decision_receipts": len(m5_receipt_payloads),
                "limit": bounded_limit,
            },
            "memory_ids": [item["id"] for item in memory_payloads],
            "tombstone_ids": [item["id"] for item in tombstone_payloads],
        }
        export_hash = _recovery_json_hash(_memory_export_integrity_payload(payload))
        payload["export_hash"] = export_hash
        artifact_path, logical_path = _recovery_artifact_path(kind="export", digest=export_hash)
        payload["artifact_path"] = logical_path
        payload["artifact_sha256"] = _recovery_json_hash(_memory_export_artifact_payload(payload))
        _write_recovery_artifact(artifact_path, payload)
        return payload

    async def rebuild_canonical_memory_index(
        self,
        *,
        actor: str,
        owner_session_id: str,
        authenticated_session_id: str,
        source_role: str = "operator",
        limit: int = _MAX_RECOVERY_RECORDS,
    ) -> dict[str, Any]:
        """Rebuild a deterministic local derived index from canonical rows.

        This path deliberately performs no embedding/provider call.  The
        resulting index is a bounded lexical identity/content digest manifest;
        semantic quality remains ``unavailable`` and is surfaced as degraded
        rather than inferred from the presence of an index file.
        """

        normalized_actor, normalized_owner = _recovery_authority(
            actor=actor,
            owner_session_id=owner_session_id,
            authenticated_session_id=authenticated_session_id,
            source_role=source_role,
        )
        bounded_limit = min(max(int(limit), 1), _MAX_RECOVERY_RECORDS)
        reconciliation = await self.reconcile_memory_tombstones(owner_session_id=normalized_owner)
        if reconciliation.get("status") != "ready":
            return {
                "schema_version": _MEMORY_INDEX_SCHEMA_VERSION,
                "owner_session_id": normalized_owner,
                "status": "degraded_no_learning",
                "degraded": True,
                "operator_status": "canonical_memory_index_rebuild_degraded",
                "no_learning_reason": "canonical tombstone ledger requires repair",
                "provenance": {
                    "kind": "operator_memory_rebuild",
                    "actor": normalized_actor,
                    "source_role": source_role,
                    "owner_session_id": normalized_owner,
                },
                "reconciliation": reconciliation,
                "records": [],
                "memory_ids": [],
                "artifact_path": None,
                "artifact_sha256": None,
                "semantic_index_status": "unavailable",
            }
        memories = [
            memory
            for memory in await self.list_memories_for_reindex(limit=bounded_limit)
            if memory.source_session_id == normalized_owner
        ]
        tombstone_revision = await self.get_memory_tombstone_revision(owner_session_id=normalized_owner)
        records: list[dict[str, Any]] = []
        for memory in memories:
            try:
                memory_metadata = json.loads(memory.metadata_json or "{}")
            except (TypeError, json.JSONDecodeError):
                memory_metadata = {}
            records.append(
                {
                    "id": memory.id,
                    "kind": _coerce_enum(memory.kind, MemoryKind).value,
                    "category": _coerce_enum(memory.category, MemoryCategory).value,
                    "content_sha256": hashlib.sha256(memory.content.encode("utf-8")).hexdigest(),
                    "source_session_id": memory.source_session_id,
                    "updated_at": _recovery_timestamp(memory.updated_at),
                    "provenance": (
                        memory_metadata.get("provenance")
                        if isinstance(memory_metadata, dict)
                        else {}
                    ),
                }
            )
        body: dict[str, Any] = {
            "schema_version": _MEMORY_INDEX_SCHEMA_VERSION,
            "owner_session_id": normalized_owner,
            "canonical_tombstone_revision": tombstone_revision,
            "retrieval_mode": "deterministic_canonical_lexical",
            "semantic_index_status": "unavailable",
            "records": records,
        }
        index_hash = _recovery_json_hash(body)
        payload = {
            **body,
            "index_hash": index_hash,
            "generated_at": _now().isoformat(),
            "status": "ready",
            "degraded": False,
            "operator_status": "canonical_memory_index_rebuilt",
            "provenance": {
                "kind": "operator_memory_rebuild",
                "actor": normalized_actor,
                "source_role": source_role,
                "owner_session_id": normalized_owner,
            },
            "no_learning_reason": "semantic index unavailable; deterministic lexical index only",
            "memory_ids": [record["id"] for record in records],
            "counts": {"records": len(records), "limit": bounded_limit},
        }
        artifact_path, logical_path = _recovery_artifact_path(kind="index", digest=index_hash)
        payload["artifact_path"] = logical_path
        payload["artifact_sha256"] = _recovery_json_hash(payload)
        _write_recovery_artifact(artifact_path, payload)
        return payload

    async def restore_canonical_memory_state(
        self,
        archive: dict[str, Any],
        *,
        actor: str,
        owner_session_id: str,
        authenticated_session_id: str,
        source_role: str = "operator",
    ) -> dict[str, Any]:
        """Restore missing canonical rows while honoring current tombstones.

        Restore is additive and tombstone-aware.  An older archive can repair a
        missing row, but it cannot overwrite a newer row or reintroduce an ID
        present in the current delete ledger.  Every archive record is checked
        before the first database write so a forged owner/session/source role
        cannot produce a partial restore.
        """

        normalized_actor, normalized_owner = _recovery_authority(
            actor=actor,
            owner_session_id=owner_session_id,
            authenticated_session_id=authenticated_session_id,
            source_role=source_role,
        )
        if not isinstance(archive, dict):
            raise ValueError("memory restore archive must be an object")
        if archive.get("schema_version") != _MEMORY_EXPORT_SCHEMA_VERSION:
            raise ValueError("unknown memory restore archive version")
        archive_owner = str(archive.get("owner_session_id") or "").strip()
        if archive_owner != normalized_owner:
            raise PermissionError("memory archive owner does not match the authenticated session")
        records = archive.get("memories")
        if not isinstance(records, list):
            raise ValueError("memory restore archive memories must be a list")
        if len(records) > _MAX_RECOVERY_RECORDS:
            raise ValueError("memory restore archive is too large")
        archive_tombstones = archive.get("tombstones", [])
        if not isinstance(archive_tombstones, list):
            raise ValueError("memory restore archive tombstones must be a list")
        if len(archive_tombstones) > _MAX_RECOVERY_RECORDS:
            raise ValueError("memory restore archive tombstones are too large")
        supplied_archive_hash = archive.get("export_hash")
        if not isinstance(supplied_archive_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", supplied_archive_hash
        ):
            raise ValueError("memory restore archive export hash is required")
        missing_envelope_fields = [
            field
            for field in _MEMORY_EXPORT_ENVELOPE_FIELDS
            if field not in archive and field not in {"m5_proposals", "m5_decision_receipts"}
        ]
        if missing_envelope_fields:
            raise ValueError(
                "memory restore archive is missing envelope fields: "
                + ", ".join(missing_envelope_fields)
            )
        expected_archive_hash = _recovery_json_hash(
            _memory_export_integrity_payload(archive)
        )
        if not hmac.compare_digest(supplied_archive_hash, expected_archive_hash):
            raise ValueError("memory restore archive hash mismatch")
        expected_artifact_path = (
            f"artifacts/memory-recovery/export-{supplied_archive_hash[:24]}.json"
        )
        if archive.get("artifact_path") != expected_artifact_path:
            raise ValueError("memory restore archive artifact path does not match its export hash")
        supplied_artifact_hash = archive.get("artifact_sha256")
        if not isinstance(supplied_artifact_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", supplied_artifact_hash
        ):
            raise ValueError("memory restore archive artifact hash is required")
        expected_artifact_hash = _recovery_json_hash(_memory_export_artifact_payload(archive))
        if not hmac.compare_digest(supplied_artifact_hash, expected_artifact_hash):
            raise ValueError("memory restore archive artifact hash mismatch")
        if archive.get("status") != "ready":
            raise ValueError("memory restore archive status is not ready")
        provenance = archive.get("provenance")
        if (
            not isinstance(provenance, dict)
            or provenance.get("actor") != normalized_actor
            or provenance.get("owner_session_id") != normalized_owner
        ):
            raise PermissionError("memory restore archive provenance owner does not match the authenticated session")
        archive_m5_proposals = archive.get("m5_proposals", [])
        if not isinstance(archive_m5_proposals, list):
            raise ValueError("memory restore archive M5 proposals must be a list")
        if len(archive_m5_proposals) > _MAX_RECOVERY_RECORDS:
            raise ValueError("memory restore archive M5 proposals are too large")
        archive_m5_receipts = archive.get("m5_decision_receipts", [])
        if not isinstance(archive_m5_receipts, list):
            raise ValueError("memory restore archive M5 decision receipts must be a list")
        if len(archive_m5_receipts) > _MAX_RECOVERY_RECORDS:
            raise ValueError("memory restore archive M5 decision receipts are too large")
        counts = archive.get("counts")
        if not isinstance(counts, dict):
            raise ValueError("memory restore archive counts must be an object")
        for count_name, actual_count in (
            ("memories", len(records)),
            ("tombstones", len(archive_tombstones)),
            ("m5_proposals", len(archive_m5_proposals)),
            ("m5_decision_receipts", len(archive_m5_receipts)),
        ):
            supplied_count = counts.get(count_name)
            if count_name in {"m5_proposals", "m5_decision_receipts"} and count_name not in counts:
                supplied_count = 0
            if isinstance(supplied_count, bool) or not isinstance(supplied_count, int) or supplied_count != actual_count:
                raise ValueError(f"memory restore archive count mismatch for {count_name}")
        memory_ids = archive.get("memory_ids")
        if not isinstance(memory_ids, list) or memory_ids != [record.get("id") for record in records]:
            raise ValueError("memory restore archive memory ids do not match records")
        tombstone_ids = archive.get("tombstone_ids")
        if not isinstance(tombstone_ids, list) or tombstone_ids != [record.get("id") for record in archive_tombstones]:
            raise ValueError("memory restore archive tombstone ids do not match records")
        normalized_m5_proposals = [
            _m5_normalize_proposal_record(
                record,
                owner_principal_id=normalized_actor,
                owner_session_id=normalized_owner,
            )
            for record in archive_m5_proposals
        ]
        normalized_m5_receipts = [
            _m5_normalize_receipt_record(
                record,
                owner_principal_id=normalized_actor,
                owner_session_id=normalized_owner,
            )
            for record in archive_m5_receipts
        ]
        if len({record["proposal_id"] for record in normalized_m5_proposals}) != len(normalized_m5_proposals):
            raise ValueError("memory restore archive contains duplicate M5 proposal ids")
        receipt_binding_digests = [record["receipt_binding_digest"] for record in normalized_m5_receipts]
        if len({record["receipt_id"] for record in normalized_m5_receipts}) != len(normalized_m5_receipts):
            raise ValueError("memory restore archive contains duplicate M5 receipt ids")
        if len(set(receipt_binding_digests)) != len(receipt_binding_digests):
            raise ValueError("memory restore archive contains duplicate M5 receipt bindings")
        normalized_tombstones: list[dict[str, Any]] = []
        seen_tombstone_ids: set[str] = set()
        seen_tombstone_memory_ids: set[str] = set()
        for tombstone in archive_tombstones:
            if not isinstance(tombstone, dict):
                raise ValueError("memory restore archive contains an invalid tombstone")
            tombstone_id = str(tombstone.get("id") or "").strip()
            tombstone_memory_id = str(tombstone.get("memory_id") or "").strip()
            if (
                not tombstone_id
                or len(tombstone_id) > 255
                or "\x00" in tombstone_id
                or not tombstone_memory_id
                or len(tombstone_memory_id) > 255
                or "\x00" in tombstone_memory_id
            ):
                raise ValueError("memory restore archive contains an invalid tombstone identity")
            if tombstone_id in seen_tombstone_ids or tombstone_memory_id in seen_tombstone_memory_ids:
                raise ValueError("memory restore archive contains duplicate tombstones")
            seen_tombstone_ids.add(tombstone_id)
            seen_tombstone_memory_ids.add(tombstone_memory_id)
            tombstone_actor = str(tombstone.get("actor") or "").strip()
            tombstone_reason = str(tombstone.get("reason") or "").strip()
            if not tombstone_actor or len(tombstone_actor) > 255 or len(tombstone_reason) > 255:
                raise ValueError("memory restore archive contains an invalid tombstone audit field")
            tombstone_created_at = _parse_recovery_timestamp(
                tombstone.get("created_at"),
                field_name="tombstone.created_at",
            )
            if tombstone_created_at is None:
                raise ValueError("memory restore archive tombstone is missing created_at")
            normalized_tombstones.append(
                {
                    "id": tombstone_id,
                    "memory_id": tombstone_memory_id,
                    "actor": tombstone_actor,
                    "reason": tombstone_reason,
                    "created_at": tombstone_created_at,
                }
            )
        normalized_records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        normalized_source_count = 0
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("memory restore archive contains an invalid record")
            memory_id = str(record.get("id") or "").strip()
            if not memory_id or len(memory_id) > 255 or "\x00" in memory_id:
                raise ValueError("memory restore archive contains an invalid memory id")
            if memory_id in seen_ids:
                raise ValueError("memory restore archive contains duplicate memory ids")
            seen_ids.add(memory_id)
            content = record.get("content")
            if not isinstance(content, str) or not content.strip() or "\x00" in content:
                raise ValueError(f"memory restore record {memory_id} has invalid content")
            if len(content) > 1_000_000:
                raise ValueError(f"memory restore record {memory_id} is too large")
            source_session_id = str(record.get("source_session_id") or "").strip()
            if not source_session_id:
                raise PermissionError(
                    f"memory restore record {memory_id} requires an owner session"
                )
            if source_session_id != normalized_owner:
                raise PermissionError(
                    f"memory restore record {memory_id} belongs to another owner session"
                )
            try:
                normalized_kind = _coerce_enum(record.get("kind"), MemoryKind)
                normalized_category = _coerce_enum(record.get("category"), MemoryCategory)
                normalized_status = _coerce_enum(record.get("status", MemoryStatus.active.value), MemoryStatus)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"memory restore record {memory_id} has an invalid enum") from exc
            created_at = _parse_recovery_timestamp(record.get("created_at"), field_name="created_at")
            updated_at = _parse_recovery_timestamp(record.get("updated_at"), field_name="updated_at")
            if created_at is None or updated_at is None:
                raise ValueError(f"memory restore record {memory_id} is missing timestamps")
            metadata = record.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError(f"memory restore record {memory_id} metadata must be an object")
            raw_marker = str(metadata.get("archived_reason") or "").strip().lower()
            raw_operator_control = metadata.get("operator_control")
            if raw_marker == _CANONICAL_MEMORY_DELETE_EXPORT_REASON or (
                isinstance(raw_operator_control, dict)
                and str(raw_operator_control.get("delete_export_state") or "").strip().lower()
                == _CANONICAL_MEMORY_REDACTED_STATE
            ):
                raise ValueError(f"memory restore record {memory_id} is a deleted canonical record")
            sources = record.get("sources", [])
            if not isinstance(sources, list):
                raise ValueError(f"memory restore record {memory_id} sources must be a list")
            if len(sources) > _MAX_RECOVERY_SOURCES_PER_RECORD:
                raise ValueError(
                    f"memory restore record {memory_id} sources exceed the per-record recovery limit"
                )
            if normalized_source_count + len(sources) > _MAX_RECOVERY_SOURCE_RECORDS:
                raise ValueError("memory restore source provenance exceeds the recovery limit")
            normalized_source_count += len(sources)
            normalized_sources: list[dict[str, Any]] = []
            for source in sources:
                if not isinstance(source, dict):
                    raise ValueError(f"memory restore record {memory_id} has an invalid source")
                source_id = str(source.get("id") or "").strip() or None
                if source_id is not None and (len(source_id) > 255 or "\x00" in source_id):
                    raise ValueError(f"memory restore record {memory_id} has an invalid source id")
                source_session = str(source.get("source_session_id") or "").strip() or normalized_owner
                if source_session != normalized_owner:
                    raise PermissionError(
                        f"memory restore source for {memory_id} belongs to another owner session"
                    )
                source_created_at = _parse_recovery_timestamp(
                    source.get("created_at"), field_name="source.created_at"
                )
                normalized_sources.append(
                    {
                        "id": source_id,
                        "source_type": str(source.get("source_type") or "session").strip() or "session",
                        "source_session_id": source_session,
                        "source_message_id": str(source.get("source_message_id") or "").strip() or None,
                        "snippet": self._normalize_source_snippet(source.get("snippet")),
                        "created_at": source_created_at,
                    }
                )
            clean_metadata = {
                str(key): value
                for key, value in metadata.items()
                if str(key) not in {"operator_control", "provenance", "privacy_boundary"}
            }
            privacy_boundary = str(metadata.get("privacy_boundary") or "operator_visible").strip().lower()
            if privacy_boundary in {"operator_visible", "private", "sensitive", "source_bound"}:
                clean_metadata["privacy_boundary"] = privacy_boundary
            clean_metadata["provenance"] = {
                "kind": "operator_memory_restore",
                "actor": normalized_actor,
                "source": "authenticated_memory_recovery",
                "owner_session_id": normalized_owner,
                "restored_at": _now().isoformat(),
            }
            clean_metadata["operator_control"] = {
                "last_action": "restore_memory",
                "last_actor": normalized_actor,
                "last_reason": "authenticated memory archive restore",
                "last_action_at": _now().isoformat(),
            }
            normalized_records.append(
                {
                    "id": memory_id,
                    "content": content.strip(),
                    "category": normalized_category,
                    "kind": normalized_kind,
                    "summary": record.get("summary") if isinstance(record.get("summary"), str) else None,
                    "confidence": _bounded_recovery_float(
                        record.get("confidence"),
                        default=0.5,
                        field_name="confidence",
                        memory_id=memory_id,
                    ),
                    "importance": _bounded_recovery_float(
                        record.get("importance"),
                        default=0.5,
                        field_name="importance",
                        memory_id=memory_id,
                    ),
                    "reinforcement": _bounded_recovery_float(
                        record.get("reinforcement"),
                        default=1.0,
                        field_name="reinforcement",
                        memory_id=memory_id,
                        maximum=None,
                    ),
                    "status": normalized_status,
                    "subject_entity_id": str(record.get("subject_entity_id") or "").strip() or None,
                    "project_entity_id": str(record.get("project_entity_id") or "").strip() or None,
                    "source_session_id": source_session_id,
                    "embedding_id": str(record.get("embedding_id") or "").strip() or None,
                    "scope_key": str(record.get("scope_key") or "").strip() or None,
                    "metadata_json": json.dumps(clean_metadata, sort_keys=True),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "last_confirmed_at": _parse_recovery_timestamp(
                        record.get("last_confirmed_at"), field_name="last_confirmed_at"
                    ),
                    "sources": normalized_sources,
                }
            )

        restored_ids: list[str] = []
        suppressed_ids: list[str] = []
        conflict_ids: list[str] = []
        owner_conflict_ids: list[str] = []
        source_count = 0
        applied_tombstone_ids: list[str] = []
        m5_restored_proposal_ids: list[str] = []
        m5_suppressed_proposal_ids: list[str] = []
        m5_blocked_proposal_ids: list[str] = []
        m5_proposal_conflict_ids: list[str] = []
        m5_restored_receipt_ids: list[str] = []
        m5_blocked_receipt_ids: list[str] = []
        m5_receipt_conflict_ids: list[str] = []
        m5_owner_conflict_ids: list[str] = []
        m5_rollback_quarantined_memory_ids: list[str] = []

        def _mark_m5_receipt_blocked(receipt_id: str) -> None:
            if receipt_id not in m5_blocked_receipt_ids:
                m5_blocked_receipt_ids.append(receipt_id)

        unverifiable_receipt_proposal_ids = {
            candidate["source_proposal_id"]
            for candidate in normalized_m5_receipts
            if candidate["source_proposal_id"] and not _m5_receipt_integrity_matches(candidate)
        }

        async def _redact_memory_for_tombstone(db, memory: Memory, tombstone: MemoryTombstone) -> None:
            try:
                metadata = json.loads(memory.metadata_json or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            operator_control = metadata.get("operator_control")
            if not isinstance(operator_control, dict):
                operator_control = {}
            operator_control.update(
                {
                    "last_action": "propagate_delete_export",
                    "last_actor": tombstone.actor,
                    "last_reason": tombstone.reason,
                    "last_action_at": _recovery_timestamp(tombstone.created_at),
                    "delete_export_state": _CANONICAL_MEMORY_REDACTED_STATE,
                    "provider_propagation_state": "runtime_receipt_only_no_full_provider_parity_claim",
                }
            )
            metadata["operator_control"] = operator_control
            metadata["archived_reason"] = _CANONICAL_MEMORY_DELETE_EXPORT_REASON
            metadata["canonical_tombstone_id"] = tombstone.id
            metadata["provenance"] = {
                "kind": "operator_propagate_delete_export",
                "actor": tombstone.actor,
                "source": "canonical_tombstone_ledger",
                "recorded_at": _recovery_timestamp(tombstone.created_at),
            }
            memory.status = MemoryStatus.archived
            memory.content = _CANONICAL_MEMORY_DELETE_CONTENT
            memory.summary = _CANONICAL_MEMORY_DELETE_CONTENT
            memory.confidence = 0.0
            memory.importance = 0.0
            memory.reinforcement = 0.0
            memory.metadata_json = json.dumps(metadata, sort_keys=True)
            memory.updated_at = tombstone.created_at
            db.add(memory)
            await db.execute(
                update(MemorySource)
                .where(MemorySource.memory_id == memory.id)
                .where(MemorySource.snippet.is_not(None))
                .values(snippet=None)
            )

        async def _m5_memory_binding_is_active(
            db,
            *,
            memory_id: str | None,
            content_digest: str | None,
            proposal_id: str | None,
            source_context_digest: str | None = None,
            decision_effect: Any | None = None,
            memory_scope: Any | None = None,
            source_binding: Any | None = None,
        ) -> bool:
            if not memory_id or not content_digest:
                return False
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            if (
                memory is None
                or str(memory.source_session_id or "").strip() != normalized_owner
                or not _canonical_memory_is_active(memory)
                or _canonical_memory_deletion_marker(memory) is not None
            ):
                return False
            tombstone = (
                await db.execute(select(MemoryTombstone).where(MemoryTombstone.memory_id == memory_id))
            ).scalars().first()
            if tombstone is not None:
                return False
            if hashlib.sha256(memory.content.encode("utf-8")).hexdigest() != content_digest:
                return False
            try:
                metadata = json.loads(memory.metadata_json or "{}")
            except (TypeError, ValueError):
                # This helper is a boolean admission gate.  Returning the
                # diagnostic string here would be truthy and could let an
                # accepted proposal survive a malformed canonical row.
                return False
            provenance = metadata.get("work_board_provenance") if isinstance(metadata, dict) else None
            if not isinstance(provenance, dict):
                return False
            if proposal_id and provenance.get("proposal_id") != proposal_id:
                return False
            if (
                provenance.get("owner_principal_id") != normalized_actor
                or provenance.get("owner_session_id") != normalized_owner
            ):
                return False
            if source_context_digest and provenance.get("source_context_digest") != source_context_digest:
                return False
            linked_proposal = None
            if decision_effect is None or memory_scope is None or source_binding is None:
                if not proposal_id:
                    return False
                linked_proposal = (
                    await db.execute(
                        select(MemoryProposal).where(
                            MemoryProposal.proposal_id == proposal_id,
                            MemoryProposal.status == MemoryProposalStatus.accepted,
                        )
                    )
                ).scalars().first()
                if linked_proposal is None:
                    return False
                decision_effect = linked_proposal.decision_effect
                try:
                    memory_scope = json.loads(linked_proposal.memory_scope_json or "{}")
                except (TypeError, ValueError):
                    return False
                if source_binding is None:
                    source_binding = linked_proposal
            if not _m5_selection_binding_matches(
                provenance,
                proposal_id=proposal_id,
                accepted_content_digest=content_digest,
                decision_effect=decision_effect,
                memory_scope=memory_scope,
                source_binding=source_binding,
            ):
                return False
            return True

        async def _m5_memory_selection_binding_state(
            db,
            *,
            memory_id: str | None,
            proposal_id: str,
            content_digest: str | None,
            decision_effect: Any,
            memory_scope: Any,
            source_binding: Any,
        ) -> bool | str | None:
            """Return canonical selection binding state for restore diagnostics.

            ``None`` means the canonical memory row is unavailable.  ``"mismatch"``
            means a row exists but its accepted selection binding disagrees;
            ``"unverifiable"`` means its keyed binding is absent or cannot be
            checked with the current server key.  These distinctions let
            restore keep tombstone/missing-row behavior while quarantining a
            forged or legacy accepted choice as blocked.
            """

            if not memory_id:
                return False
            memory = (
                await db.execute(select(Memory).where(Memory.id == memory_id))
            ).scalars().first()
            if memory is None:
                return None
            if str(memory.source_session_id or "").strip() != normalized_owner:
                return None
            if _canonical_memory_deletion_marker(memory) is not None:
                return None
            if (
                await db.execute(
                    select(MemoryTombstone).where(MemoryTombstone.memory_id == memory_id)
                )
            ).scalars().first() is not None:
                return None
            try:
                metadata = json.loads(memory.metadata_json or "{}")
            except (TypeError, ValueError):
                return "unverifiable"
            provenance = metadata.get("work_board_provenance") if isinstance(metadata, dict) else None
            if _m5_selection_binding_matches(
                provenance,
                proposal_id=proposal_id,
                accepted_content_digest=content_digest,
                decision_effect=decision_effect,
                memory_scope=memory_scope,
                source_binding=source_binding,
            ):
                return True
            return _m5_selection_binding_failure_reason(
                provenance,
                proposal_id=proposal_id,
                accepted_content_digest=content_digest,
                decision_effect=decision_effect,
                memory_scope=memory_scope,
                source_binding=source_binding,
            )

        async with self._canonical_memory_lock:
            async with get_session() as db:
                await _begin_canonical_write(db)
                current_tombstone_result = await db.execute(select(MemoryTombstone))
                current_tombstones = current_tombstone_result.scalars().all()
                current_by_memory = {
                    tombstone.memory_id: tombstone for tombstone in current_tombstones
                }
                current_by_id = {tombstone.id: tombstone for tombstone in current_tombstones}
                # Rollback is a canonical operator decision, but unlike a
                # deletion it is represented by the M5 proposal/memory
                # projections and their audit event.  Keep those markers as
                # restore barriers so a public archive rehash cannot turn a
                # rolled-back decision back into an active one.
                rolled_back_memory_ids: set[str] = set()
                rolled_back_proposal_ids: set[str] = set()
                archive_rollback_proposal_ids: set[str] = set()
                archive_unverifiable_rollback_proposal_ids: set[str] = set()
                archive_rollback_markers: dict[str, dict[str, Any]] = {}
                current_rolled_back_proposals = (
                    await db.execute(
                        select(MemoryProposal).where(
                            MemoryProposal.owner_principal_id == normalized_actor,
                            MemoryProposal.owner_session_id == normalized_owner,
                            MemoryProposal.status == MemoryProposalStatus.rolled_back,
                        )
                    )
                ).scalars().all()
                for proposal in current_rolled_back_proposals:
                    rolled_back_proposal_ids.add(proposal.proposal_id)
                    if proposal.accepted_memory_id:
                        rolled_back_memory_ids.add(proposal.accepted_memory_id)
                rollback_audits = (
                    await db.execute(
                        select(AuditEvent).where(
                            AuditEvent.actor == normalized_actor,
                            AuditEvent.session_id == normalized_owner,
                            AuditEvent.event_type == "memory_learning_rolled_back",
                        )
                    )
                ).scalars().all()
                for audit in rollback_audits:
                    try:
                        audit_details = json.loads(audit.details_json or "{}")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(audit_details, dict):
                        continue
                    audit_proposal_id = audit_details.get("proposal_id")
                    audit_memory_id = audit_details.get("accepted_memory_id")
                    if isinstance(audit_proposal_id, str) and audit_proposal_id.strip():
                        rolled_back_proposal_ids.add(audit_proposal_id.strip())
                    if isinstance(audit_memory_id, str) and audit_memory_id.strip():
                        rolled_back_memory_ids.add(audit_memory_id.strip())
                archive_tombstones_applied = False
                for candidate in normalized_tombstones:
                    memory = (
                        await db.execute(
                            select(Memory).where(Memory.id == candidate["memory_id"])
                        )
                    ).scalars().first()
                    if memory is None:
                        raise ValueError(
                            f"memory restore tombstone {candidate['memory_id']} has no canonical row"
                        )
                    memory_owner = str(memory.source_session_id or "").strip()
                    if not memory_owner:
                        raise PermissionError(
                            f"memory restore tombstone {candidate['memory_id']} has no owner session"
                        )
                    if memory_owner != normalized_owner:
                        raise PermissionError(
                            f"memory restore tombstone {candidate['memory_id']} belongs to another owner session"
                        )
                    existing_by_id = current_by_id.get(candidate["id"])
                    if existing_by_id is not None and existing_by_id.memory_id != candidate["memory_id"]:
                        raise ValueError(
                            f"memory restore tombstone id {candidate['id']} belongs to another memory"
                        )
                    tombstone = current_by_memory.get(candidate["memory_id"])
                    if tombstone is None:
                        tombstone = MemoryTombstone(**candidate)
                        db.add(tombstone)
                        await db.flush()
                        current_by_memory[candidate["memory_id"]] = tombstone
                        current_by_id[tombstone.id] = tombstone
                        applied_tombstone_ids.append(tombstone.id)
                    await _redact_memory_for_tombstone(db, memory, tombstone)
                    archive_tombstones_applied = True
                if archive_tombstones_applied:
                    await db.execute(
                        update(MemorySnapshot)
                        .values(content="", source_hash=None, updated_at=_now())
                    )
                    await db.flush()
                tombstoned_ids = set(current_by_memory)
                proposal_archive_by_id = {
                    candidate["proposal_id"]: candidate
                    for candidate in normalized_m5_proposals
                }
                for record in normalized_records:
                    memory_id = record["id"]
                    existing = (
                        await db.execute(select(Memory).where(Memory.id == memory_id))
                    ).scalars().first()
                    current_rollback_proposal_id = (
                        _m5_rollback_proposal_id_from_memory(existing)
                        if existing is not None
                        else None
                    )
                    if current_rollback_proposal_id:
                        rolled_back_memory_ids.add(memory_id)
                        rolled_back_proposal_ids.add(current_rollback_proposal_id)
                    archive_rollback_marker = _m5_rollback_marker_from_memory(record)
                    archive_rollback_proposal_id = (
                        _m5_rollback_proposal_id_from_memory(record)
                        if archive_rollback_marker is not None
                        else None
                    )
                    try:
                        record_metadata = json.loads(record["metadata_json"] or "{}")
                    except (TypeError, ValueError):
                        record_metadata = {}
                    record_provenance = (
                        record_metadata.get("work_board_provenance")
                        if isinstance(record_metadata, dict)
                        else None
                    )
                    if archive_rollback_proposal_id is None and isinstance(record_provenance, dict):
                        lifecycle_proposal_id = record_provenance.get("proposal_id")
                        if isinstance(lifecycle_proposal_id, str) and lifecycle_proposal_id.strip():
                            archive_rollback_proposal_id = lifecycle_proposal_id.strip()
                    rollback_marker_state = "absent"
                    if archive_rollback_proposal_id and archive_rollback_marker is not None:
                        rollback_marker_state = _m5_rollback_marker_state(
                            record,
                            memory_id=memory_id,
                            proposal_id=archive_rollback_proposal_id,
                            accepted_content_digest=hashlib.sha256(
                                record["content"].encode("utf-8")
                            ).hexdigest(),
                            owner_principal_id=normalized_actor,
                            owner_session_id=normalized_owner,
                        )
                    proposal_archive = proposal_archive_by_id.get(archive_rollback_proposal_id or "")
                    proposal_digest = str(
                        proposal_archive.get("accepted_memory_content_digest") or ""
                    ).strip().lower() if proposal_archive else ""
                    record_digest = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
                    linked_rollback = bool(
                        proposal_archive
                        and proposal_archive.get("accepted_memory_id") == memory_id
                        and proposal_digest == record_digest
                    )
                    lifecycle_state = (
                        _m5_rollback_lifecycle_state(
                            record_provenance,
                            proposal_id=archive_rollback_proposal_id or "",
                            accepted_content_digest=record_digest,
                            owner_principal_id=normalized_actor,
                            owner_session_id=normalized_owner,
                        )
                        if linked_rollback
                        else "unverifiable"
                        if isinstance(record_provenance, dict)
                        and record_provenance.get("lifecycle_state") == "rolled_back"
                        else "absent"
                    )
                    if rollback_marker_state == "valid" or lifecycle_state == "valid":
                        # Either signed record can preserve a terminal rollback.
                        # The selection signature remains authoritative even if
                        # an archive editor removes the separate marker and
                        # recomputes the public envelope hashes.
                        archive_rollback_proposal_ids.add(archive_rollback_proposal_id)
                        if rollback_marker_state == "valid":
                            archive_rollback_markers[archive_rollback_proposal_id] = dict(
                                archive_rollback_marker or {}
                            )
                        else:
                            archive_rollback_markers[archive_rollback_proposal_id] = {
                                "rollback_at": record_provenance.get("lifecycle_at"),
                                "rollback_reason": record_provenance.get("lifecycle_reason"),
                            }
                        record["status"] = MemoryStatus.archived
                    elif (
                        rollback_marker_state == "unverifiable"
                        or lifecycle_state in {"mismatch", "unverifiable"}
                    ):
                        # An unverifiable rollback projection must not restore
                        # as active. Keep the record quarantined for explicit
                        # source verification and a fresh operator decision.
                        archive_unverifiable_rollback_proposal_ids.add(
                            archive_rollback_proposal_id or ""
                        )
                        if memory_id not in m5_rollback_quarantined_memory_ids:
                            m5_rollback_quarantined_memory_ids.append(memory_id)
                        continue
                    # A structurally complete active-key marker with a bad
                    # MAC is a public-archive forgery. Ignore it when the
                    # signed accepted-memory lifecycle remains active.
                    if memory_id in tombstoned_ids or memory_id in rolled_back_memory_ids:
                        suppressed_ids.append(memory_id)
                        continue
                    if existing is not None and _canonical_memory_deletion_marker(existing) is not None:
                        suppressed_ids.append(memory_id)
                        continue
                    if existing is not None:
                        existing_owner = str(existing.source_session_id or "").strip()
                        if existing_owner != normalized_owner:
                            # A same-ID archive record cannot claim or replace
                            # a row owned by another session (including an
                            # unbound legacy row).  Quarantine it in the
                            # restore receipt and leave its content, owner,
                            # timestamps, and provenance untouched.
                            owner_conflict_ids.append(memory_id)
                            continue
                        existing_updated_at = _normalize_utc_timestamp(existing.updated_at)
                        incoming_updated_at = _normalize_utc_timestamp(record["updated_at"])
                        if (
                            existing_updated_at is not None
                            and incoming_updated_at is not None
                            and existing_updated_at >= incoming_updated_at
                        ):
                            conflict_ids.append(memory_id)
                            continue
                        existing.content = record["content"]
                        existing.category = record["category"]
                        existing.kind = record["kind"]
                        existing.summary = record["summary"]
                        existing.confidence = record["confidence"]
                        existing.importance = record["importance"]
                        existing.reinforcement = record["reinforcement"]
                        existing.status = record["status"]
                        existing.subject_entity_id = record["subject_entity_id"]
                        existing.project_entity_id = record["project_entity_id"]
                        existing.source_session_id = record["source_session_id"]
                        existing.embedding_id = record["embedding_id"]
                        existing.scope_key = record["scope_key"]
                        existing.metadata_json = record["metadata_json"]
                        existing_created_at = _normalize_utc_timestamp(existing.created_at)
                        incoming_created_at = _normalize_utc_timestamp(record["created_at"])
                        if existing_created_at is None:
                            existing.created_at = incoming_created_at
                        elif incoming_created_at is not None:
                            existing.created_at = min(existing_created_at, incoming_created_at)
                        existing.updated_at = incoming_updated_at
                        existing.last_confirmed_at = record["last_confirmed_at"]
                        db.add(existing)
                    else:
                        db.add(
                            Memory(
                                id=memory_id,
                                content=record["content"],
                                category=record["category"],
                                kind=record["kind"],
                                summary=record["summary"],
                                confidence=record["confidence"],
                                importance=record["importance"],
                                reinforcement=record["reinforcement"],
                                status=record["status"],
                                subject_entity_id=record["subject_entity_id"],
                                project_entity_id=record["project_entity_id"],
                                source_session_id=record["source_session_id"],
                                embedding_id=record["embedding_id"],
                                scope_key=record["scope_key"],
                                metadata_json=record["metadata_json"],
                                created_at=record["created_at"],
                                updated_at=record["updated_at"],
                                last_confirmed_at=record["last_confirmed_at"],
                            )
                        )
                    await db.flush()
                    restored_ids.append(memory_id)
                    for source in record["sources"]:
                        if source["id"]:
                            existing_source_id = (
                                await db.execute(
                                    select(MemorySource).where(MemorySource.id == source["id"])
                                )
                            ).scalars().first()
                            if existing_source_id is not None:
                                if existing_source_id.memory_id != memory_id:
                                    raise ValueError(
                                        f"memory restore source id {source['id']} belongs to another memory"
                                    )
                                continue
                        duplicate_statement = select(MemorySource).where(
                            MemorySource.memory_id == memory_id,
                            MemorySource.source_type == source["source_type"],
                            MemorySource.source_session_id == source["source_session_id"],
                            MemorySource.source_message_id == source["source_message_id"],
                        )
                        duplicate = (await db.execute(duplicate_statement)).scalars().first()
                        if duplicate is not None:
                            continue
                        db.add(
                            MemorySource(
                                **({"id": source["id"]} if source["id"] else {}),
                                memory_id=memory_id,
                                source_type=source["source_type"],
                                source_session_id=source["source_session_id"],
                                source_message_id=source["source_message_id"],
                                snippet=source["snippet"],
                                **(
                                    {"created_at": source["created_at"]}
                                    if source["created_at"] is not None
                                    else {}
                                ),
                            )
                        )
                        source_count += 1
                    await db.flush()

                # An accepted proposal is only a recoverable decision source
                # when its original verified source has an authenticated
                # baseline receipt in the same archive.  Public envelope
                # hashes do not authenticate this relationship, so validate it
                # before any proposal can be inserted.
                proposal_by_id = {
                    candidate["proposal_id"]: candidate
                    for candidate in normalized_m5_proposals
                }
                # Receipt bindings describe the accepted source journey.  A
                # restore may quarantine or roll back its proposal projection,
                # but that lifecycle mutation must not invalidate the
                # independently authenticated source-baseline receipt needed
                # for explicit re-verification.
                proposal_binding_by_id: dict[str, dict[str, Any]] = {}
                for candidate in normalized_m5_proposals:
                    binding_candidate = dict(candidate)
                    if binding_candidate["status"] in {
                        MemoryProposalStatus.blocked,
                        MemoryProposalStatus.rolled_back,
                    }:
                        binding_candidate["status"] = MemoryProposalStatus.accepted
                    proposal_binding_by_id[candidate["proposal_id"]] = binding_candidate
                source_baseline_state: dict[str, str] = {}
                for receipt_candidate in normalized_m5_receipts:
                    if (
                        receipt_candidate["receipt_stage"]
                        is not WorkBoardDecisionReceiptStage.source_baseline
                        or not receipt_candidate["source_proposal_id"]
                    ):
                        continue
                    proposal_candidate = proposal_by_id.get(
                        receipt_candidate["source_proposal_id"]
                    )
                    state = "unverifiable"
                    if proposal_candidate is not None:
                        binding_proposal_candidate = proposal_binding_by_id.get(
                            receipt_candidate["source_proposal_id"],
                            proposal_candidate,
                        )
                        if not _m5_receipt_integrity_matches(receipt_candidate):
                            state = "unverifiable"
                        elif not _m5_receipt_binding_matches(
                            receipt_candidate,
                            binding_proposal_candidate,
                        ):
                            state = "mismatch"
                        elif (
                            receipt_candidate["decision_status"]
                            is WorkBoardDecisionStatus.blocked
                        ):
                            state = "unverifiable"
                        else:
                            state = "valid"
                    previous_state = source_baseline_state.get(
                        receipt_candidate["source_proposal_id"]
                    )
                    if previous_state != "valid":
                        source_baseline_state[receipt_candidate["source_proposal_id"]] = state

                # Restore only the content-free M5 projections.  Raw proposal
                # text/provenance/source bodies are intentionally absent.  A
                # bounded matching scope and safe evidence identifiers may be
                # restored after validation.
                for candidate in normalized_m5_proposals:
                    proposal_state_before = {
                        field: candidate[field]
                        for field in (
                            "status",
                            "reason_code",
                            "recovery_action",
                            "rollback_by_principal_id",
                            "rollback_by_session_id",
                            "rollback_at",
                            "rollback_reason",
                        )
                    }
                    rollback_authoritative = candidate["proposal_id"] in (
                        rolled_back_proposal_ids | archive_rollback_proposal_ids
                    )
                    rollback_unverifiable = candidate["proposal_id"] in (
                        archive_unverifiable_rollback_proposal_ids
                    )
                    if rollback_authoritative:
                        # Keep a canonical rollback terminal.  A public archive
                        # can change the enum and rebuild its hashes, but it
                        # cannot override the current proposal/memory marker or
                        # the rollback audit event.
                        candidate["status"] = MemoryProposalStatus.rolled_back
                        candidate["reason_code"] = "rolled_back"
                        candidate["recovery_action"] = "none"
                        archive_marker = archive_rollback_markers.get(candidate["proposal_id"])
                        if archive_marker is not None:
                            candidate["rollback_by_principal_id"] = normalized_actor
                            candidate["rollback_by_session_id"] = normalized_owner
                            candidate["rollback_at"] = _parse_recovery_timestamp(
                                archive_marker.get("rollback_at"),
                                field_name="work_board_rollback.rollback_at",
                            )
                            candidate["rollback_reason"] = str(archive_marker.get("rollback_reason") or "")
                    elif rollback_unverifiable:
                        if candidate["privacy_state"] is MemoryProposalPrivacyState.redacted:
                            m5_suppressed_proposal_ids.append(candidate["proposal_id"])
                            continue
                        candidate["status"] = MemoryProposalStatus.blocked
                        candidate["reason_code"] = "rollback_binding_unverifiable"
                        candidate["recovery_action"] = "verify_source_and_reaccept"
                        if candidate["proposal_id"] not in m5_blocked_proposal_ids:
                            m5_blocked_proposal_ids.append(candidate["proposal_id"])
                    elif candidate["status"] is MemoryProposalStatus.accepted:
                        if candidate["privacy_state"] is MemoryProposalPrivacyState.redacted:
                            m5_suppressed_proposal_ids.append(candidate["proposal_id"])
                            continue
                        baseline_state = source_baseline_state.get(candidate["proposal_id"])
                        if baseline_state != "valid":
                            candidate["status"] = MemoryProposalStatus.blocked
                            candidate["reason_code"] = {
                                None: "source_baseline_missing",
                                "mismatch": "source_baseline_binding_mismatch",
                                "unverifiable": "source_baseline_integrity_unverifiable",
                            }.get(
                                baseline_state,
                                "source_baseline_integrity_unverifiable",
                            )
                            candidate["recovery_action"] = "verify_source_and_reaccept"
                            m5_blocked_proposal_ids.append(candidate["proposal_id"])
                            # Without an authenticated source baseline there is
                            # no verified journey to recover, even if the
                            # accepted memory row itself is still present.
                            binding_active = False
                            selection_binding_state = "unverifiable"
                        elif candidate["proposal_id"] in unverifiable_receipt_proposal_ids:
                            candidate["status"] = MemoryProposalStatus.blocked
                            candidate["reason_code"] = "receipt_integrity_unverifiable"
                            candidate["recovery_action"] = "verify_source_and_reaccept"
                            m5_blocked_proposal_ids.append(candidate["proposal_id"])
                            # The source receipt is not trustworthy enough to
                            # allow this proposal to become a decision source.
                            # Keep the projection visible for re-review.
                            binding_active = False
                            selection_binding_state = "unverifiable"
                        else:
                            binding_active = None
                            selection_binding_state = None
                        selection_scope = None
                        try:
                            selection_scope = json.loads(candidate["memory_scope_json"] or "{}")
                        except (TypeError, ValueError):
                            selection_scope = None
                        if binding_active is None:
                            binding_active = await _m5_memory_binding_is_active(
                                db,
                                memory_id=candidate["accepted_memory_id"],
                                content_digest=candidate["accepted_memory_content_digest"],
                                proposal_id=candidate["proposal_id"],
                                source_context_digest=candidate["source_context_digest"],
                                decision_effect=candidate["decision_effect"],
                                memory_scope=selection_scope,
                                source_binding=candidate,
                            )
                        if not binding_active:
                            if selection_binding_state is None:
                                selection_binding_state = await _m5_memory_selection_binding_state(
                                    db,
                                    memory_id=candidate["accepted_memory_id"],
                                    proposal_id=candidate["proposal_id"],
                                    content_digest=candidate["accepted_memory_content_digest"],
                                    decision_effect=candidate["decision_effect"],
                                    memory_scope=selection_scope,
                                    source_binding=candidate,
                                )
                            if selection_binding_state in {"mismatch", "unverifiable", False}:
                                # A present row with a failed selection MAC is
                                # historical data, not an active memory. Keep
                                # it visible for operator recovery while
                                # ensuring general memory queries cannot treat
                                # a rehashed archive as an accepted decision.
                                candidate["status"] = MemoryProposalStatus.blocked
                                invalid_memory = (
                                    await db.execute(
                                        select(Memory).where(
                                            Memory.id == candidate["accepted_memory_id"]
                                        )
                                    )
                                ).scalars().first()
                                if (
                                    invalid_memory is not None
                                    and invalid_memory.source_session_id == normalized_owner
                                    and _canonical_memory_deletion_marker(invalid_memory) is None
                                ):
                                    invalid_memory.status = MemoryStatus.archived
                                    invalid_memory.updated_at = _now()
                                    db.add(invalid_memory)
                                if (
                                    candidate["proposal_id"] not in unverifiable_receipt_proposal_ids
                                    and baseline_state == "valid"
                                ):
                                    candidate["reason_code"] = (
                                        "accepted_memory_binding_unverifiable"
                                        if selection_binding_state == "unverifiable"
                                        else "accepted_memory_binding_mismatch"
                                    )
                                    candidate["recovery_action"] = (
                                        "verify_source_and_reaccept"
                                        if selection_binding_state == "unverifiable"
                                        else "request_verified_proposal_again"
                                    )
                                if candidate["proposal_id"] not in m5_blocked_proposal_ids:
                                    m5_blocked_proposal_ids.append(candidate["proposal_id"])
                            else:
                                m5_suppressed_proposal_ids.append(candidate["proposal_id"])
                            # A missing/tombstoned memory keeps the existing
                            # suppression behavior; a present mismatched row
                            # stays archived until source review and acceptance.
                            if selection_binding_state not in {"mismatch", "unverifiable", False}:
                                continue
                    elif candidate["status"] is MemoryProposalStatus.blocked:
                        m5_blocked_proposal_ids.append(candidate["proposal_id"])
                    if any(
                        candidate[field] != previous
                        for field, previous in proposal_state_before.items()
                    ):
                        candidate["revision"] = max(1, int(candidate["revision"] or 0)) + 1
                        candidate["updated_at"] = _now()
                    existing = (
                        await db.execute(
                            select(MemoryProposal).where(
                                MemoryProposal.proposal_id == candidate["proposal_id"]
                            )
                        )
                    ).scalars().first()
                    if existing is not None:
                        if (
                            existing.owner_principal_id != normalized_actor
                            or existing.owner_session_id != normalized_owner
                        ):
                            m5_owner_conflict_ids.append(candidate["proposal_id"])
                        elif existing.request_binding_digest != candidate["request_binding_digest"]:
                            m5_proposal_conflict_ids.append(candidate["proposal_id"])
                        else:
                            m5_proposal_conflict_ids.append(candidate["proposal_id"])
                        continue
                    if (
                        candidate["preview_text_digest"]
                        and candidate["status"]
                        not in {MemoryProposalStatus.blocked, MemoryProposalStatus.expired}
                    ):
                        duplicate_preview = (
                            await db.execute(
                                select(MemoryProposal).where(
                                    MemoryProposal.owner_principal_id == normalized_actor,
                                    MemoryProposal.owner_session_id == normalized_owner,
                                    MemoryProposal.source_task_id == candidate["source_task_id"],
                                    MemoryProposal.source_attempt_id == candidate["source_attempt_id"],
                                    MemoryProposal.preview_text_digest == candidate["preview_text_digest"],
                                    MemoryProposal.status.notin_(
                                        [MemoryProposalStatus.blocked, MemoryProposalStatus.expired]
                                    ),
                                )
                            )
                        ).scalars().first()
                        if duplicate_preview is not None:
                            m5_proposal_conflict_ids.append(candidate["proposal_id"])
                            continue
                    if candidate["status"] is MemoryProposalStatus.no_learning:
                        duplicate_no_learning = (
                            await db.execute(
                                select(MemoryProposal).where(
                                    MemoryProposal.owner_principal_id == normalized_actor,
                                    MemoryProposal.owner_session_id == normalized_owner,
                                    MemoryProposal.source_task_id == candidate["source_task_id"],
                                    MemoryProposal.source_attempt_id == candidate["source_attempt_id"],
                                    MemoryProposal.status == MemoryProposalStatus.no_learning,
                                )
                            )
                        ).scalars().first()
                        if duplicate_no_learning is not None:
                            m5_proposal_conflict_ids.append(candidate["proposal_id"])
                            continue
                    db.add(MemoryProposal(**candidate))
                    await db.flush()
                    m5_restored_proposal_ids.append(candidate["proposal_id"])

                # A recovery child is eligible only while its blocked/expired
                # source proposal is present and the complete recovery link
                # still authenticates. Validate after the proposal batch so
                # archive ordering cannot affect the result.
                from src.memory.m5 import _m5_authenticated_recovery_link

                for candidate in normalized_m5_proposals:
                    if (
                        candidate["proposal_id"] not in m5_restored_proposal_ids
                        or not candidate["recovered_from_proposal_id"]
                    ):
                        continue
                    recovered = (
                        await db.execute(
                            select(MemoryProposal).where(
                                MemoryProposal.proposal_id == candidate["proposal_id"],
                                MemoryProposal.owner_principal_id == normalized_actor,
                                MemoryProposal.owner_session_id == normalized_owner,
                            )
                        )
                    ).scalars().first()
                    if recovered is None or recovered.status is not MemoryProposalStatus.accepted:
                        continue
                    recovery_parent = (
                        await db.execute(
                            select(MemoryProposal).where(
                                MemoryProposal.proposal_id
                                == recovered.recovered_from_proposal_id,
                                MemoryProposal.owner_principal_id == normalized_actor,
                                MemoryProposal.owner_session_id == normalized_owner,
                            )
                        )
                    ).scalars().first()
                    recovered_memory = (
                        await db.execute(
                            select(Memory).where(Memory.id == recovered.accepted_memory_id)
                        )
                    ).scalars().first()
                    if (
                        recovery_parent is not None
                        and recovered_memory is not None
                        and _m5_authenticated_recovery_link(
                            recovery_parent,
                            recovered,
                            recovered_memory,
                        )
                    ):
                        continue
                    recovered.status = MemoryProposalStatus.blocked
                    recovered.reason_code = "recovery_parent_binding_mismatch"
                    recovered.recovery_action = "request_verified_proposal_again"
                    recovered.revision = int(recovered.revision or 0) + 1
                    recovered.updated_at = _now()
                    db.add(recovered)
                    if (
                        recovered_memory is not None
                        and recovered_memory.source_session_id == normalized_owner
                        and recovered_memory.status is MemoryStatus.active
                        and _canonical_memory_deletion_marker(recovered_memory) is None
                    ):
                        recovered_memory.status = MemoryStatus.archived
                        recovered_memory.updated_at = _now()
                        db.add(recovered_memory)
                    if recovered.proposal_id not in m5_blocked_proposal_ids:
                        m5_blocked_proposal_ids.append(recovered.proposal_id)
                    await db.flush()

                for candidate in normalized_m5_receipts:
                    receipt_candidate = dict(candidate)
                    receipt_state_before = {
                        field: receipt_candidate[field]
                        for field in ("decision_status", "admission_status", "reason")
                    }
                    receipt_integrity_valid = _m5_receipt_integrity_matches(receipt_candidate)
                    linked_proposal = None
                    if receipt_candidate["source_proposal_id"]:
                        linked_proposal = (
                            await db.execute(
                                select(MemoryProposal).where(
                                    MemoryProposal.proposal_id == receipt_candidate["source_proposal_id"],
                                    MemoryProposal.owner_principal_id == normalized_actor,
                                    MemoryProposal.owner_session_id == normalized_owner,
                                )
                            )
                        ).scalars().first()
                    binding_proposal = linked_proposal
                    if receipt_candidate["source_proposal_id"]:
                        binding_proposal = proposal_binding_by_id.get(
                            receipt_candidate["source_proposal_id"],
                            binding_proposal,
                        )
                    if not receipt_integrity_valid:
                        receipt_candidate["decision_status"] = WorkBoardDecisionStatus.blocked
                        receipt_candidate["admission_status"] = WorkBoardDecisionAdmissionStatus.blocked
                        receipt_candidate["reason"] = "receipt_integrity_unverifiable"
                        _mark_m5_receipt_blocked(receipt_candidate["receipt_id"])
                    elif not _m5_receipt_binding_matches(receipt_candidate, binding_proposal):
                        receipt_candidate["decision_status"] = WorkBoardDecisionStatus.blocked
                        receipt_candidate["admission_status"] = WorkBoardDecisionAdmissionStatus.blocked
                        receipt_candidate["reason"] = "receipt_binding_mismatch"
                        _mark_m5_receipt_blocked(receipt_candidate["receipt_id"])
                    elif (
                        receipt_candidate["receipt_stage"]
                        is WorkBoardDecisionReceiptStage.later_comparison
                        and receipt_candidate["source_proposal_id"]
                        and (
                        linked_proposal is None
                            or linked_proposal.status
                            in {
                                MemoryProposalStatus.blocked,
                                MemoryProposalStatus.rolled_back,
                            }
                        )
                    ):
                        receipt_candidate["decision_status"] = WorkBoardDecisionStatus.blocked
                        receipt_candidate["admission_status"] = WorkBoardDecisionAdmissionStatus.blocked
                        receipt_candidate["reason"] = "source_proposal_unavailable_on_restore"
                        _mark_m5_receipt_blocked(receipt_candidate["receipt_id"])
                    if (
                        receipt_candidate["receipt_stage"]
                        is WorkBoardDecisionReceiptStage.later_comparison
                        and receipt_candidate["decision_status"]
                        is not WorkBoardDecisionStatus.blocked
                        and receipt_candidate["accepted_memory_id"]
                        and receipt_integrity_valid
                    ):
                        memory_available = await _m5_memory_binding_is_active(
                            db,
                            memory_id=receipt_candidate["accepted_memory_id"],
                            content_digest=receipt_candidate["accepted_memory_content_digest"],
                            proposal_id=receipt_candidate["source_proposal_id"],
                        )
                        if not memory_available:
                            receipt_candidate["decision_status"] = WorkBoardDecisionStatus.blocked
                            receipt_candidate["admission_status"] = WorkBoardDecisionAdmissionStatus.blocked
                            receipt_candidate["reason"] = "accepted_memory_unavailable_on_restore"
                            _mark_m5_receipt_blocked(receipt_candidate["receipt_id"])
                    receipt_mutated = any(
                        receipt_candidate[field] != previous
                        for field, previous in receipt_state_before.items()
                    )
                    if receipt_mutated:
                        receipt_candidate["revision"] = max(
                            1,
                            int(receipt_candidate["revision"] or 0),
                        ) + 1
                        receipt_candidate["updated_at"] = _now()
                    if receipt_integrity_valid and receipt_mutated:
                        # A valid receipt may be blocked by a current source or
                        # memory check during restore.  Seal that local,
                        # operator-visible state with the same server key.
                        try:
                            receipt_candidate["receipt_integrity_mac"] = _m5_receipt_integrity_mac(
                                receipt_candidate
                            )
                        except CapabilityJournalError:
                            receipt_candidate["receipt_integrity_mac"] = None
                    existing = (
                        await db.execute(
                            select(WorkBoardDecisionReceipt).where(
                                WorkBoardDecisionReceipt.receipt_id == receipt_candidate["receipt_id"]
                            )
                        )
                    ).scalars().first()
                    if existing is not None:
                        if (
                            existing.owner_principal_id != normalized_actor
                            or existing.owner_session_id != normalized_owner
                        ):
                            m5_owner_conflict_ids.append(receipt_candidate["receipt_id"])
                        else:
                            m5_receipt_conflict_ids.append(receipt_candidate["receipt_id"])
                        continue
                    duplicate_binding = (
                        await db.execute(
                            select(WorkBoardDecisionReceipt).where(
                                WorkBoardDecisionReceipt.receipt_binding_digest
                                == receipt_candidate["receipt_binding_digest"]
                            )
                        )
                    ).scalars().first()
                    if duplicate_binding is not None:
                        m5_receipt_conflict_ids.append(receipt_candidate["receipt_id"])
                        continue
                    db.add(WorkBoardDecisionReceipt(**receipt_candidate))
                    await db.flush()
                    m5_restored_receipt_ids.append(receipt_candidate["receipt_id"])
        reconciliation = await self.reconcile_memory_tombstones(owner_session_id=normalized_owner)
        if owner_conflict_ids:
            reconciliation = {
                **reconciliation,
                "status": "degraded",
                "reason": "memory restore encountered records owned by another session",
                "owner_conflict_count": len(owner_conflict_ids),
                "owner_conflict_memory_ids": owner_conflict_ids,
            }
        status = "ready" if reconciliation.get("status") == "ready" else "degraded_no_learning"
        no_learning_reason = (
            "memory restore owner conflicts require operator review"
            if owner_conflict_ids
            else (
                None
                if status == "ready"
                else "canonical tombstone ledger requires repair"
            )
        )
        return {
            "schema_version": _MEMORY_EXPORT_SCHEMA_VERSION,
            "status": status,
            "operator_status": "canonical_memory_restore_ready" if status == "ready" else "canonical_memory_restore_degraded",
            "no_learning_reason": no_learning_reason,
            "provenance": {
                "kind": "operator_memory_restore",
                "actor": normalized_actor,
                "source_role": source_role,
                "owner_session_id": normalized_owner,
            },
            "archive_hash": archive.get("export_hash"),
            "restored_memory_ids": restored_ids,
            "tombstone_suppressed_memory_ids": suppressed_ids,
            "newer_conflict_memory_ids": conflict_ids,
            "restored_count": len(restored_ids),
            "tombstone_suppressed_count": len(suppressed_ids),
            "newer_conflict_count": len(conflict_ids),
            "owner_conflict_memory_ids": owner_conflict_ids,
            "owner_conflict_count": len(owner_conflict_ids),
            "conflict_memory_ids": [*conflict_ids, *owner_conflict_ids],
            "conflict_count": len(conflict_ids) + len(owner_conflict_ids),
            "m5_restored_proposal_ids": m5_restored_proposal_ids,
            "m5_suppressed_proposal_ids": m5_suppressed_proposal_ids,
            "m5_blocked_proposal_ids": m5_blocked_proposal_ids,
            "m5_proposal_conflict_ids": m5_proposal_conflict_ids,
            "m5_restored_receipt_ids": m5_restored_receipt_ids,
            "m5_blocked_receipt_ids": m5_blocked_receipt_ids,
            "m5_receipt_conflict_ids": m5_receipt_conflict_ids,
            "m5_owner_conflict_ids": m5_owner_conflict_ids,
            "m5_rollback_quarantined_memory_ids": m5_rollback_quarantined_memory_ids,
            "m5_restored_proposal_count": len(m5_restored_proposal_ids),
            "m5_suppressed_proposal_count": len(m5_suppressed_proposal_ids),
            "m5_blocked_proposal_count": len(m5_blocked_proposal_ids),
            "m5_restored_receipt_count": len(m5_restored_receipt_ids),
            "m5_blocked_receipt_count": len(m5_blocked_receipt_ids),
            "restored_source_count": source_count,
            "applied_archive_tombstone_ids": applied_tombstone_ids,
            "current_tombstone_revision": await self.get_memory_tombstone_revision(
                owner_session_id=normalized_owner
            ),
            "reconciliation": reconciliation,
        }

    async def mark_memory_tombstoned(
        self,
        memory_id: str,
        *,
        actor: str,
        reason: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
        deleted_at: datetime | None = None,
    ) -> MemoryTombstoneWriteResult:
        """Serialize repeated local delete requests for one canonical row."""

        normalized_memory_id = str(memory_id or "").strip()
        lock = self._get_scoped_memory_lock(
            kind=MemoryKind.fact,
            scope={"purpose": "canonical_tombstone", "memory_id": normalized_memory_id},
        )
        async with lock:
            async with self._canonical_memory_lock:
                return await self._mark_memory_tombstoned(
                    memory_id,
                    actor=actor,
                    reason=reason,
                    metadata_updates=metadata_updates,
                    deleted_at=deleted_at,
                )

    async def _mark_memory_tombstoned(
        self,
        memory_id: str,
        *,
        actor: str,
        reason: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
        deleted_at: datetime | None = None,
    ) -> MemoryTombstoneWriteResult:
        """Atomically redact a canonical row and record its delete authority.

        Repeating the operation is idempotent: the first actor/reason/time in
        the ledger remain authoritative while the canonical redaction is
        re-applied.  The ledger has no content field so a restore cannot use
        it to recover deleted material.
        """

        normalized_memory_id = str(memory_id or "").strip()
        normalized_actor = str(actor or "").strip()
        normalized_reason = str(reason or _CANONICAL_MEMORY_DELETE_EXPORT_REASON).strip()
        if not normalized_memory_id:
            raise ValueError("memory_id must be non-empty")
        if not normalized_actor:
            raise ValueError("actor must be non-empty")
        if not normalized_reason:
            normalized_reason = _CANONICAL_MEMORY_DELETE_EXPORT_REASON
        if len(normalized_actor) > 255 or len(normalized_reason) > 255:
            raise ValueError("actor and reason must be at most 255 characters")
        deletion_time = deleted_at or _now()
        if deletion_time.tzinfo is None:
            deletion_time = deletion_time.replace(tzinfo=timezone.utc)
        else:
            deletion_time = deletion_time.astimezone(timezone.utc)

        async with get_session() as db:
            await _begin_canonical_write(db)
            memory = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {normalized_memory_id}")

            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == normalized_memory_id
                    )
                )
            ).scalars().first()
            created = tombstone is None
            if tombstone is None:
                tombstone = MemoryTombstone(
                    memory_id=normalized_memory_id,
                    actor=normalized_actor,
                    reason=normalized_reason,
                    created_at=deletion_time,
                )
                db.add(tombstone)
                try:
                    # The process-local lock covers the normal path.  Keep a
                    # database-level retry as well for two workers with
                    # separate repository instances racing on the unique key.
                    await db.flush()
                except IntegrityError:
                    await db.rollback()
                    await _begin_canonical_write(db)
                    tombstone = (
                        await db.execute(
                            select(MemoryTombstone).where(
                                MemoryTombstone.memory_id == normalized_memory_id
                            )
                        )
                    ).scalars().first()
                    memory = (
                        await db.execute(
                            select(Memory).where(Memory.id == normalized_memory_id)
                        )
                    ).scalars().first()
                    if tombstone is None or memory is None:
                        raise
                    created = False
            # Once the ledger row exists its actor, reason, and timestamp are
            # immutable audit authority.  A repeated request may reapply
            # redaction, but must not replace first-request provenance with a
            # later caller's metadata.
            updates = dict(metadata_updates or {}) if created else {}
            try:
                metadata = json.loads(memory.metadata_json or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.update(updates)
            metadata["canonical_tombstone_id"] = tombstone.id
            metadata["archived_reason"] = _CANONICAL_MEMORY_DELETE_EXPORT_REASON
            memory.status = MemoryStatus.archived
            memory.content = _CANONICAL_MEMORY_DELETE_CONTENT
            memory.summary = _CANONICAL_MEMORY_DELETE_CONTENT
            memory.confidence = 0.0
            memory.importance = 0.0
            memory.reinforcement = 0.0
            memory.metadata_json = json.dumps(metadata, sort_keys=True)
            memory.updated_at = tombstone.created_at
            db.add(memory)
            await db.execute(
                update(MemorySource)
                .where(MemorySource.memory_id == normalized_memory_id)
                .where(MemorySource.snippet.is_not(None))
                .values(snippet=None)
            )
            # M5 proposal previews and decision receipts are a privacy
            # projection of canonical memory.  The tombstone wins in the
            # same immediate transaction and invalidates any pending/confirmed
            # dispatch effect before commit.
            from src.memory.m5 import redact_m5_memory_references

            await redact_m5_memory_references(db, normalized_memory_id)
            await db.execute(
                update(MemorySnapshot)
                .values(content="", source_hash=None, updated_at=tombstone.created_at)
            )
            await db.flush()
            db.expunge(memory)
            db.expunge(tombstone)
            return MemoryTombstoneWriteResult(
                memory=memory,
                tombstone=tombstone,
                created=created,
            )

    async def reconcile_memory_tombstones(
        self,
        *,
        owner_session_id: str | None = None,
        _acquire_lock: bool = True,
    ) -> dict[str, int | str]:
        """Re-apply durable canonical deletion authority after a stale restore.

        The result is a deterministic operator receipt.  ``degraded`` means a
        ledger row refers to a missing memory row and therefore requires
        operator restore repair; no deleted content is returned.  Recovery
        callers pass their authenticated owner so receipt counts and revision
        inputs cannot disclose another owner's tombstone activity.
        """

        normalized_owner = str(owner_session_id or "").strip()
        if _acquire_lock:
            async with self._canonical_memory_lock:
                return await self.reconcile_memory_tombstones(
                    owner_session_id=normalized_owner or None,
                    _acquire_lock=False,
                )

        checked_count = 0
        reapplied_count = 0
        missing_memory_count = 0
        async with get_session() as db:
            await _begin_canonical_write(db)
            tombstone_statement = select(MemoryTombstone)
            if normalized_owner:
                tombstone_statement = tombstone_statement.join(
                    Memory,
                    Memory.id == MemoryTombstone.memory_id,
                ).where(Memory.source_session_id == normalized_owner)
            tombstones = (
                await db.execute(
                    tombstone_statement.order_by(
                        col(MemoryTombstone.created_at).asc(),
                        col(MemoryTombstone.id).asc(),
                    )
                )
            ).scalars().all()
            for tombstone in tombstones:
                checked_count += 1
                memory_statement = select(Memory).where(Memory.id == tombstone.memory_id)
                if normalized_owner:
                    memory_statement = memory_statement.where(Memory.source_session_id == normalized_owner)
                memory = (await db.execute(memory_statement)).scalars().first()
                if memory is None:
                    missing_memory_count += 1
                    continue
                try:
                    metadata = json.loads(memory.metadata_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                operator_control = metadata.get("operator_control")
                if not isinstance(operator_control, dict):
                    operator_control = {}
                operator_control.update(
                    {
                        "last_action": "propagate_delete_export",
                        "last_actor": tombstone.actor,
                        "last_reason": tombstone.reason,
                        "last_action_at": tombstone.created_at.isoformat(),
                        "delete_export_state": _CANONICAL_MEMORY_REDACTED_STATE,
                        "provider_propagation_state": "runtime_receipt_only_no_full_provider_parity_claim",
                    }
                )
                metadata["operator_control"] = operator_control
                metadata["archived_reason"] = _CANONICAL_MEMORY_DELETE_EXPORT_REASON
                metadata["canonical_tombstone_id"] = tombstone.id
                metadata["provenance"] = {
                    "kind": "operator_propagate_delete_export",
                    "actor": tombstone.actor,
                    "source": "canonical_tombstone_ledger",
                    "recorded_at": tombstone.created_at.isoformat(),
                }
                source_redaction = await db.execute(
                    update(MemorySource)
                    .where(MemorySource.memory_id == tombstone.memory_id)
                    .where(MemorySource.snippet.is_not(None))
                    .values(snippet=None)
                )
                needs_reapply = any(
                    (
                        memory.status != MemoryStatus.archived,
                        memory.content != _CANONICAL_MEMORY_DELETE_CONTENT,
                        memory.summary != _CANONICAL_MEMORY_DELETE_CONTENT,
                        float(memory.confidence or 0.0) != 0.0,
                        float(memory.importance or 0.0) != 0.0,
                        float(memory.reinforcement or 0.0) != 0.0,
                        memory.metadata_json != json.dumps(metadata, sort_keys=True),
                    )
                )
                if needs_reapply or source_redaction.rowcount > 0:
                    memory.status = MemoryStatus.archived
                    memory.content = _CANONICAL_MEMORY_DELETE_CONTENT
                    memory.summary = _CANONICAL_MEMORY_DELETE_CONTENT
                    memory.confidence = 0.0
                    memory.importance = 0.0
                    memory.reinforcement = 0.0
                    memory.metadata_json = json.dumps(metadata, sort_keys=True)
                    memory.updated_at = _now()
                    db.add(memory)
                    reapplied_count += 1
            if reapplied_count > 0:
                await db.execute(
                    update(MemorySnapshot)
                    .values(content="", source_hash=None, updated_at=_now())
                )
            await db.flush()
        return {
            "schema_version": "guardian.memory_tombstone.v1",
            "status": "degraded" if missing_memory_count else "ready",
            "checked_count": checked_count,
            "reapplied_count": reapplied_count,
            "missing_memory_count": missing_memory_count,
        }

    async def list_memories_for_reindex(self, *, limit: int = 10_000) -> list[Memory]:
        """Return active canonical rows safe for a local deterministic reindex.

        Reconciliation runs first, so a stale active row from restore is
        redacted before it can be admitted to a derived index.
        """

        bounded_limit = min(max(int(limit), 1), 10_000)
        async with self._canonical_memory_lock:
            reconciliation = await self.reconcile_memory_tombstones(_acquire_lock=False)
            if reconciliation.get("status") != "ready":
                return []
            async with get_session() as db:
                result = await db.execute(
                    select(Memory)
                    .outerjoin(MemoryTombstone, MemoryTombstone.memory_id == Memory.id)
                    .where(Memory.status == MemoryStatus.active)
                    .where(MemoryTombstone.id.is_(None))
                    .order_by(col(Memory.updated_at).asc(), col(Memory.id).asc())
                    .limit(bounded_limit)
                )
                memories = result.scalars().all()
                memories = [
                    memory
                    for memory in memories
                    if _canonical_memory_deletion_marker(memory) is None
                ]
                for memory in memories:
                    db.expunge(memory)
                return list(memories)

    async def update_memory_control_metadata(
        self,
        memory_id: str,
        *,
        status: MemoryStatus | str | None = None,
        content: str | None = None,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        reinforcement: float | None = None,
        metadata_updates: dict[str, Any] | None = None,
        last_confirmed_at: datetime | None = None,
    ) -> Memory:
        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            raise ValueError("memory_id must be non-empty")

        def _normalize_timestamp(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        async with get_session() as db:
            await _begin_canonical_write(db)
            memory = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if memory is None:
                raise ValueError(f"Unknown memory id: {normalized_memory_id}")

            requested_status = (
                _coerce_enum(status, MemoryStatus)
                if status is not None
                else None
            )
            tombstone = (
                await db.execute(
                    select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == normalized_memory_id
                    )
                )
            ).scalars().first()
            if tombstone is not None or _canonical_memory_deletion_marker(memory) is not None:
                if requested_status is MemoryStatus.active:
                    raise ValueError(
                        "cannot reactivate canonical memory after operator delete/export redaction"
                    )
                raise ValueError(
                    "cannot mutate canonical memory after operator delete/export redaction"
                )

            expected_status = _coerce_enum(memory.status, MemoryStatus)
            expected_metadata = memory.metadata_json
            expected_updated_at = _normalize_timestamp(memory.updated_at)
            expected_metadata_guard = (
                Memory.metadata_json.is_(None)
                if expected_metadata is None
                else Memory.metadata_json == expected_metadata
            )
            expected_updated_at_guard = (
                Memory.updated_at.is_(None)
                if expected_updated_at is None
                else Memory.updated_at == expected_updated_at
            )

            if status is not None:
                memory.status = requested_status
            if isinstance(content, str):
                normalized_content = content.strip()
                if not normalized_content:
                    raise ValueError("content must be non-empty")
                memory.content = normalized_content
            if isinstance(summary, str):
                memory.summary = summary.strip() or None
            if confidence is not None:
                memory.confidence = max(0.0, min(1.0, float(confidence)))
            if importance is not None:
                memory.importance = max(0.0, min(1.0, float(importance)))
            if reinforcement is not None:
                memory.reinforcement = max(0.0, float(reinforcement))
            if last_confirmed_at is not None:
                memory.last_confirmed_at = _normalize_timestamp(last_confirmed_at)

            metadata: dict[str, Any]
            try:
                parsed_metadata = json.loads(memory.metadata_json or "{}")
            except json.JSONDecodeError:
                parsed_metadata = {}
            metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
            if metadata_updates:
                metadata.update(metadata_updates)
                memory.metadata_json = json.dumps(metadata, sort_keys=True)

            memory.updated_at = _now()
            db.expunge(memory)
            guarded_update = await db.execute(
                update(Memory)
                .where(
                    Memory.id == normalized_memory_id,
                    Memory.status == expected_status,
                    expected_metadata_guard,
                    expected_updated_at_guard,
                    _canonical_memory_without_tombstone_clause(),
                )
                .values(
                    status=requested_status or expected_status,
                    content=memory.content,
                    summary=memory.summary,
                    confidence=memory.confidence,
                    importance=memory.importance,
                    reinforcement=memory.reinforcement,
                    last_confirmed_at=memory.last_confirmed_at,
                    metadata_json=memory.metadata_json,
                    updated_at=memory.updated_at,
                )
            )
            if guarded_update.rowcount != 1:
                raise ValueError(
                    "memory changed before control update; canonical deletion or another memory control won"
                )
            memory = (
                await db.execute(
                    select(Memory).where(Memory.id == normalized_memory_id)
                )
            ).scalars().one()
            db.expunge(memory)
            return memory

    async def rollback_memory_if_unchanged(
        self,
        memory_id: str,
        *,
        expected_updated_at: datetime,
        expected_metadata_json: str | None,
        expected_status: MemoryStatus | str,
        confidence: float,
        importance: float,
        reinforcement: float,
        last_confirmed_at: datetime,
        metadata_updates: dict[str, Any],
    ) -> Memory:
        """Restore one memory only when its control snapshot is unchanged.

        The conditional update closes the read-then-write race with
        delete/export: if another control commits after the caller reads the
        memory, the expected timestamp or metadata no longer matches and this
        method performs no activation.
        """

        normalized_memory_id = str(memory_id or "").strip()
        if not normalized_memory_id:
            raise ValueError("memory_id must be non-empty")
        normalized_status = _coerce_enum(expected_status, MemoryStatus)
        expected_metadata = expected_metadata_json
        try:
            parsed_metadata = json.loads(expected_metadata or "{}")
        except json.JSONDecodeError:
            parsed_metadata = {}
        metadata = parsed_metadata if isinstance(parsed_metadata, dict) else {}
        metadata.update(metadata_updates or {})

        def _normalize_timestamp(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        expected_metadata_guard = (
            Memory.metadata_json.is_(None)
            if expected_metadata is None
            else Memory.metadata_json == expected_metadata
        )
        async with get_session() as db:
            await _begin_canonical_write(db)
            existing = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if existing is not None and _canonical_memory_deletion_marker(existing) is not None:
                raise ValueError(
                    "cannot rollback canonical memory after operator delete/export redaction"
                )
            result = await db.execute(
                update(Memory)
                .where(
                    Memory.id == normalized_memory_id,
                    Memory.updated_at == _normalize_timestamp(expected_updated_at),
                    Memory.status == normalized_status,
                    expected_metadata_guard,
                    _canonical_memory_without_tombstone_clause(),
                )
                .values(
                    status=MemoryStatus.active,
                    confidence=max(0.0, min(1.0, float(confidence))),
                    importance=max(0.0, min(1.0, float(importance))),
                    reinforcement=max(0.0, float(reinforcement)),
                    last_confirmed_at=_normalize_timestamp(last_confirmed_at),
                    metadata_json=json.dumps(metadata, sort_keys=True),
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise ValueError(
                    "memory changed before rollback; canonical deletion or another "
                    "memory control won"
                )
            memory = (
                await db.execute(select(Memory).where(Memory.id == normalized_memory_id))
            ).scalars().first()
            if memory is None:  # pragma: no cover - guarded update matched a row
                raise ValueError(f"Unknown memory id: {normalized_memory_id}")
            db.expunge(memory)
            return memory

    async def update_memory_control(
        self,
        memory_id: str,
        *,
        status: MemoryStatus | str | None = None,
        content: str | None = None,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        reinforcement: float | None = None,
        metadata: dict[str, Any] | None = None,
        last_confirmed_at: datetime | None = None,
    ) -> Memory:
        return await self.update_memory_control_metadata(
            memory_id,
            status=status,
            content=content,
            summary=summary,
            confidence=confidence,
            importance=importance,
            reinforcement=reinforcement,
            metadata_updates=metadata,
            last_confirmed_at=last_confirmed_at,
        )

    async def list_memories_for_scope(
        self,
        *,
        kind: MemoryKind | str,
        scope: dict[str, Any],
        limit: int = 20,
        status: MemoryStatus | str = MemoryStatus.active,
        exact_scope_keys: tuple[str, ...] = (),
    ) -> list[Memory]:
        normalized_kind = _coerce_enum(kind, MemoryKind)
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_scope = {
            str(key): value
            for key, value in (scope or {}).items()
            if str(key).strip() and value is not None
        }
        if not normalized_scope:
            raise ValueError("scope must contain at least one key")
        normalized_scope_key = self._scoped_memory_key(
            kind=normalized_kind,
            scope=normalized_scope,
        )
        normalized_exact_scope_keys = tuple(
            dict.fromkeys(str(key).strip() for key in exact_scope_keys if str(key).strip())
        )

        def _matches_exact_scope_keys(metadata: dict[str, Any]) -> bool:
            if not normalized_exact_scope_keys:
                return True
            return all(
                (
                    metadata.get(key) == normalized_scope[key]
                    if key in normalized_scope
                    else metadata.get(key) is None
                )
                for key in normalized_exact_scope_keys
            )

        matches: list[Memory] = []
        async with get_session() as db:
            exact_stmt = (
                select(Memory)
                .where(Memory.kind == normalized_kind)
                .where(Memory.status == normalized_status)
                .where(Memory.scope_key == normalized_scope_key)
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
                .limit(limit)
            )
            exact_stmt = exact_stmt.where(_canonical_memory_without_tombstone_clause())
            exact_result = await db.execute(exact_stmt)
            for memory in exact_result.scalars().all():
                if _canonical_memory_deletion_marker(memory) is not None:
                    continue
                try:
                    metadata = json.loads(memory.metadata_json or "{}")
                except json.JSONDecodeError:
                    continue
                if not isinstance(metadata, dict):
                    continue
                if not _matches_exact_scope_keys(metadata):
                    continue
                db.expunge(memory)
                matches.append(memory)
            if matches:
                return matches

            legacy_stmt = (
                select(Memory)
                .where(Memory.kind == normalized_kind)
                .where(Memory.status == normalized_status)
                .where(func.json_valid(Memory.metadata_json) == 1)
                .where(func.json_type(Memory.metadata_json, "$") == "object")
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
                .limit(limit)
            )
            legacy_stmt = legacy_stmt.where(_canonical_memory_without_tombstone_clause())
            for key, value in normalized_scope.items():
                legacy_stmt = legacy_stmt.where(
                    func.json_extract(Memory.metadata_json, _sqlite_json_object_path(key)) == value
                )
            result = await db.execute(legacy_stmt)
            for memory in result.scalars().all():
                if _canonical_memory_deletion_marker(memory) is not None:
                    continue
                try:
                    metadata = json.loads(memory.metadata_json or "{}")
                except json.JSONDecodeError:
                    continue
                if not isinstance(metadata, dict):
                    continue
                if not all(metadata.get(key) == value for key, value in normalized_scope.items()):
                    continue
                if not _matches_exact_scope_keys(metadata):
                    continue
                db.expunge(memory)
                matches.append(memory)
        return matches

    async def list_memories_for_entities(
        self,
        *,
        subject_entity_ids: tuple[str, ...] = (),
        project_entity_ids: tuple[str, ...] = (),
        kinds: tuple[MemoryKind | str, ...] = (),
        limit: int = 20,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> list[Memory]:
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_subject_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in subject_entity_ids if entity_id.strip())
        )
        normalized_project_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in project_entity_ids if entity_id.strip())
        )
        normalized_kinds = tuple(
            dict.fromkeys(_coerce_enum(kind, MemoryKind) for kind in kinds)
        )
        if not normalized_subject_ids and not normalized_project_ids:
            return []
        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.status == normalized_status)
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
                .limit(limit)
            )
            stmt = stmt.where(_canonical_memory_without_tombstone_clause())
            filters = []
            if normalized_subject_ids:
                filters.append(col(Memory.subject_entity_id).in_(normalized_subject_ids))
            if normalized_project_ids:
                filters.append(col(Memory.project_entity_id).in_(normalized_project_ids))
            stmt = stmt.where(or_(*filters))
            if normalized_kinds:
                stmt = stmt.where(col(Memory.kind).in_(normalized_kinds))
            result = await db.execute(stmt)
            memories = [
                memory
                for memory in result.scalars().all()
                if _canonical_memory_deletion_marker(memory) is None
            ]
            for memory in memories:
                db.expunge(memory)
            return list(memories)

    async def list_memories_by_kinds(
        self,
        *,
        kinds: tuple[MemoryKind | str, ...],
        limit_per_kind: int = 3,
        status: MemoryStatus | str = MemoryStatus.active,
    ) -> dict[str, list[Memory]]:
        normalized_status = _coerce_enum(status, MemoryStatus)
        normalized_kinds = tuple(dict.fromkeys(_coerce_enum(kind, MemoryKind) for kind in kinds))
        if not normalized_kinds:
            return {}

        async with get_session() as db:
            stmt = (
                select(Memory)
                .where(Memory.status == normalized_status)
                .where(col(Memory.kind).in_(normalized_kinds))
                .order_by(
                    col(Memory.importance).desc(),
                    col(Memory.last_confirmed_at).desc(),
                    col(Memory.created_at).desc(),
                )
            )
            stmt = stmt.where(_canonical_memory_without_tombstone_clause())
            result = await db.execute(stmt)
            grouped: dict[str, list[Memory]] = {kind.value: [] for kind in normalized_kinds}
            for memory in result.scalars().all():
                if _canonical_memory_deletion_marker(memory) is not None:
                    continue
                bucket = grouped.setdefault(memory.kind.value, [])
                if len(bucket) >= limit_per_kind:
                    continue
                db.expunge(memory)
                bucket.append(memory)
            return {key: value for key, value in grouped.items() if value}

    async def list_episodes(
        self,
        *,
        session_id: str | None = None,
        episode_types: tuple[MemoryEpisodeType | str, ...] = (),
        subject_entity_ids: tuple[str, ...] = (),
        project_entity_ids: tuple[str, ...] = (),
        limit: int = 20,
    ) -> list[MemoryEpisode]:
        normalized_episode_types = tuple(
            dict.fromkeys(_coerce_enum(item, MemoryEpisodeType) for item in episode_types)
        )
        normalized_subject_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in subject_entity_ids if entity_id.strip())
        )
        normalized_project_ids = tuple(
            dict.fromkeys(entity_id.strip() for entity_id in project_entity_ids if entity_id.strip())
        )
        async with get_session() as db:
            stmt = (
                select(MemoryEpisode)
                .order_by(
                    col(MemoryEpisode.observed_at).desc(),
                    col(MemoryEpisode.salience).desc(),
                    col(MemoryEpisode.created_at).desc(),
                )
                .limit(limit)
            )
            if session_id is not None:
                stmt = stmt.where(MemoryEpisode.session_id == session_id)
            if normalized_episode_types:
                stmt = stmt.where(col(MemoryEpisode.episode_type).in_(normalized_episode_types))
            entity_filters = []
            if normalized_subject_ids:
                entity_filters.append(col(MemoryEpisode.subject_entity_id).in_(normalized_subject_ids))
            if normalized_project_ids:
                entity_filters.append(col(MemoryEpisode.project_entity_id).in_(normalized_project_ids))
            if entity_filters:
                stmt = stmt.where(or_(*entity_filters))
            result = await db.execute(stmt)
            episodes = result.scalars().all()
            for episode in episodes:
                db.expunge(episode)
            return list(episodes)

    async def create_edge(
        self,
        *,
        from_memory_id: str,
        to_memory_id: str,
        edge_type: MemoryEdgeType | str = MemoryEdgeType.related,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEdge:
        normalized_from_memory_id = str(from_memory_id or "").strip()
        normalized_to_memory_id = str(to_memory_id or "").strip()
        if not normalized_from_memory_id or not normalized_to_memory_id:
            raise ValueError("from_memory_id and to_memory_id must be non-empty")
        normalized_edge_type = _coerce_enum(edge_type, MemoryEdgeType)
        endpoint_ids = {normalized_from_memory_id, normalized_to_memory_id}
        async with self._canonical_memory_lock:
            async with get_session() as db:
                # Edge creation is a canonical write.  Serialize the endpoint
                # check with tombstone writes so a deleted endpoint cannot be
                # admitted between the check and the insert.
                await _begin_canonical_write(db)
                endpoint_result = await db.execute(
                    select(Memory).where(
                        Memory.id.in_(endpoint_ids),
                        _canonical_memory_without_tombstone_clause(),
                    )
                )
                canonical_endpoint_ids = {
                    memory.id
                    for memory in endpoint_result.scalars().all()
                    if _canonical_memory_deletion_marker(memory) is None
                }
                if canonical_endpoint_ids != endpoint_ids:
                    raise ValueError(
                        "edge endpoints must reference existing canonical memories"
                    )

                existing = (
                    await db.execute(
                        select(MemoryEdge)
                        .where(MemoryEdge.from_memory_id == normalized_from_memory_id)
                        .where(MemoryEdge.to_memory_id == normalized_to_memory_id)
                        .where(MemoryEdge.edge_type == normalized_edge_type)
                    )
                ).scalars().first()
                if existing is not None:
                    db.expunge(existing)
                    return existing
                edge = MemoryEdge(
                    from_memory_id=normalized_from_memory_id,
                    to_memory_id=normalized_to_memory_id,
                    edge_type=normalized_edge_type,
                    weight=weight,
                    metadata_json=json.dumps(metadata or {}, sort_keys=True),
                )
                db.add(edge)
                await db.flush()
                db.expunge(edge)
                return edge

    async def list_edges(
        self,
        *,
        from_memory_id: str | None = None,
        to_memory_id: str | None = None,
        edge_type: MemoryEdgeType | str | None = None,
    ) -> list[MemoryEdge]:
        async with get_session() as db:
            # Tombstones are canonical authority.  Keep both endpoint
            # predicates in the same read transaction so a stale edge never
            # becomes context merely because its row survived deletion.
            stmt = (
                select(MemoryEdge)
                .where(
                    ~exists().where(
                        MemoryTombstone.memory_id == MemoryEdge.from_memory_id
                    ),
                    ~exists().where(
                        MemoryTombstone.memory_id == MemoryEdge.to_memory_id
                    ),
                )
                .order_by(col(MemoryEdge.created_at).asc())
            )
            if from_memory_id is not None:
                stmt = stmt.where(
                    MemoryEdge.from_memory_id == str(from_memory_id).strip()
                )
            if to_memory_id is not None:
                stmt = stmt.where(
                    MemoryEdge.to_memory_id == str(to_memory_id).strip()
                )
            if edge_type is not None:
                stmt = stmt.where(MemoryEdge.edge_type == _coerce_enum(edge_type, MemoryEdgeType))
            result = await db.execute(stmt)
            edges = result.scalars().all()
            endpoint_ids = {
                memory_id
                for edge in edges
                for memory_id in (edge.from_memory_id, edge.to_memory_id)
            }
            if endpoint_ids:
                endpoint_result = await db.execute(
                    select(Memory).where(Memory.id.in_(endpoint_ids))
                )
                canonical_endpoint_ids = {
                    memory.id
                    for memory in endpoint_result.scalars().all()
                    if _canonical_memory_deletion_marker(memory) is None
                }
                edges = [
                    edge
                    for edge in edges
                    if edge.from_memory_id in canonical_endpoint_ids
                    and edge.to_memory_id in canonical_endpoint_ids
                ]
            for edge in edges:
                db.expunge(edge)
            return list(edges)

    async def save_snapshot(
        self,
        *,
        kind: MemorySnapshotKind | str = MemorySnapshotKind.bounded_guardian_context,
        content: str,
        source_hash: str | None = None,
        canonical_tombstone_revision: str | None = None,
    ) -> MemorySnapshot:
        normalized_kind = _coerce_enum(kind, MemorySnapshotKind)
        reconciliation = await self.reconcile_memory_tombstones()
        if reconciliation.get("status") != "ready":
            raise RuntimeError("canonical memory authority unavailable for snapshot write")
        async with get_session() as db:
            await _begin_canonical_write(db)
            current_revision = await _memory_tombstone_revision(db)
            if canonical_tombstone_revision is None:
                if current_revision != _EMPTY_TOMBSTONE_REVISION:
                    raise RuntimeError(
                        "snapshot write requires a canonical tombstone revision"
                    )
            elif canonical_tombstone_revision != current_revision:
                raise RuntimeError(
                    "canonical memory changed before snapshot write"
                )
            result = await db.execute(
                select(MemorySnapshot).where(MemorySnapshot.kind == normalized_kind)
            )
            snapshot = result.scalars().first()
            if snapshot is None:
                snapshot = MemorySnapshot(
                    kind=normalized_kind,
                    content=content,
                    source_hash=source_hash,
                    canonical_tombstone_revision=current_revision,
                )
            else:
                snapshot.content = content
                snapshot.source_hash = source_hash
                snapshot.canonical_tombstone_revision = current_revision
                snapshot.updated_at = _now()
            db.add(snapshot)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                await _begin_canonical_write(db)
                snapshot = (
                    await db.execute(
                        select(MemorySnapshot).where(MemorySnapshot.kind == normalized_kind)
                    )
                ).scalars().one()
                snapshot.content = content
                snapshot.source_hash = source_hash
                snapshot.canonical_tombstone_revision = current_revision
                snapshot.updated_at = _now()
                db.add(snapshot)
                await db.flush()
            db.expunge(snapshot)
            return snapshot

    async def get_snapshot(self, kind: MemorySnapshotKind | str = MemorySnapshotKind.bounded_guardian_context) -> MemorySnapshot | None:
        normalized_kind = _coerce_enum(kind, MemorySnapshotKind)
        reconciliation = await self.reconcile_memory_tombstones()
        if reconciliation.get("status") != "ready":
            return None
        async with get_session() as db:
            await _begin_canonical_write(db)
            current_revision = await _memory_tombstone_revision(db)
            result = await db.execute(
                select(MemorySnapshot).where(MemorySnapshot.kind == normalized_kind)
            )
            snapshot = result.scalars().first()
            if snapshot is not None:
                snapshot_revision = snapshot.canonical_tombstone_revision
                if (
                    snapshot_revision != current_revision
                    and not (
                        snapshot_revision is None
                        and current_revision == _EMPTY_TOMBSTONE_REVISION
                    )
                ):
                    snapshot.content = ""
                    snapshot.source_hash = None
                    snapshot.canonical_tombstone_revision = current_revision
                    snapshot.updated_at = _now()
                    await db.flush()
                    return None
            if snapshot is not None:
                db.expunge(snapshot)
            return snapshot


memory_repository = MemoryRepository()
