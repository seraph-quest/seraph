"""M5 verified outcome memory and same-card decision guard.

This module is deliberately small and provider free at the decision boundary.
It consumes the existing work-board proof and canonical Memory tables.  The
memory may suggest a bounded registered capability; it never supplies a
capability, grant, input, or external approval.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import ValidationError

from src.db.engine import get_session
from src.db.models import (
    AuditEvent,
    Memory,
    MemoryCategory,
    MemoryEdge,
    MemoryEdgeType,
    MemoryKind,
    MemoryProposal,
    MemoryProposalDecisionEffect,
    MemoryProposalPrivacyState,
    MemoryProposalProviderContactState,
    MemoryProposalStatus,
    MemorySource,
    MemoryStatus,
    MemoryTombstone,
    Goal,
    Session,
    WorkBoardAttempt,
    WorkBoardDecisionAdmissionStatus,
    WorkBoardDecisionReceipt,
    WorkBoardDecisionReceiptStage,
    WorkBoardDecisionStatus,
    WorkBoardProposal,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.memory.repository import (
    _canonical_memory_deletion_marker,
    _m5_receipt_binding_matches,
    _m5_receipt_integrity_mac,
    _m5_receipt_integrity_matches,
    _m5_selection_binding_key_id,
    _m5_selection_binding_mac,
    _m5_verified_source_binding,
)
from src.memory.repository import memory_repository
from src.extensions.capability_execution import CapabilityJournalError
from src.vault import redaction as vault_redaction

M5_SCHEMA_VERSION = "memory_proposal.v1"
M5_RECEIPT_SCHEMA_VERSION = "work_board_decision_receipt.v1"
M5_SCOPE_SCHEMA_VERSION = "memory_scope.v1"
M5_PROVENANCE_SCHEMA_VERSION = "work_board_provenance.v1"
M5_DIGEST_VERSION = "seraph.m5-canonical-json.v1"
M5_MAX_TEXT = 2_000
M5_DISCOVERY_LIMIT = 2
M5_NONE_PROPOSAL = "proposal:none"
M5_NONE_MEMORY = "memory:none"
M5_NONE_DIGEST = "digest:none"
M5_NONE_ACTION = "action:none"
M5_NO_LEARNING = "no_learning"
M5_MAX_CANDIDATES = 20

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|bearer|password|passwd|secret|private[_ -]?key)\s*[:=]"
)
_AUTHORITY_TEXT = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?previous|system\s+message|you\s+are\s+an?\s+|grant\s+(?:me\s+)?(?:permission|authority)|approve\s+this|allow\s+execution|administrator\s+override)"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _canonical_json_value(value: Any) -> Any:
    """Recursively normalize the M5 JSON digest input without ``default=str``."""

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("M5 digests reject non-finite numbers")
        return value
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    raise TypeError(f"unsupported M5 digest value: {type(value).__name__}")


def m5_canonical_json(value: Any) -> str:
    normalized = _canonical_json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def m5_digest(value: Any) -> str:
    return hashlib.sha256(m5_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_m5_memory_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("M5 memory text must be a string")
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not normalized:
        raise ValueError("M5 memory text must be non-empty")
    if len(normalized) > M5_MAX_TEXT:
        raise ValueError("M5 memory text exceeds 2,000 Unicode scalar values")
    return normalized


def m5_text_digest(value: str) -> str:
    return hashlib.sha256(normalize_m5_memory_text(value).encode("utf-8")).hexdigest()


def sanitize_m5_memory_text(value: str) -> str:
    """Reject content that could become a secret or an authority instruction."""

    normalized = normalize_m5_memory_text(value)
    if _SECRET_ASSIGNMENT.search(normalized) or _AUTHORITY_TEXT.search(normalized):
        raise ValueError("memory proposal contains secret or authority-bearing text")
    return normalized


async def sanitize_m5_memory_text_async(value: str) -> str:
    redacted = await vault_redaction.redact_secrets_in_text(value, fail_closed=True)
    if redacted == "[redaction unavailable]":
        raise ValueError("memory proposal redaction is unavailable")
    return sanitize_m5_memory_text(redacted)


def _safe_identifier(value: Any, *, field: str) -> str:
    candidate = str(value or "").strip()
    if not candidate or not _SAFE_ID.fullmatch(candidate):
        raise ValueError(f"{field} is not a safe identifier")
    return candidate


def _safe_digest(value: Any, *, field: str, allow_empty: bool = False) -> str:
    candidate = str(value or "").strip().lower()
    if allow_empty and not candidate:
        return ""
    if not _DIGEST.fullmatch(candidate):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return candidate


def _decode_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _decode_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _capability_version(capability_id: str) -> str:
    try:
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES

        spec = REGISTERED_CAPABILITIES.get(capability_id)
        return str(spec.version) if spec is not None else ""
    except Exception:
        return ""


def m5_registered_capability_options() -> list[dict[str, str]]:
    """Expose only registered capability identities for operator review."""

    try:
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES, _TYPED_INPUT_MODELS
    except ImportError:
        return []
    return [
        {"capability_id": capability_id, "version": str(spec.version)}
        for capability_id, spec in sorted(REGISTERED_CAPABILITIES.items())
        if capability_id in _TYPED_INPUT_MODELS
    ]


def m5_registered_capability_contracts() -> list[dict[str, Any]]:
    """Expose static typed schemas for operator-authored proposal candidates."""

    try:
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES, _TYPED_INPUT_MODELS
    except ImportError:
        return []
    result: list[dict[str, Any]] = []
    for capability_id, spec in sorted(REGISTERED_CAPABILITIES.items()):
        input_model = _TYPED_INPUT_MODELS.get(capability_id)
        if input_model is None:
            continue
        result.append(
            {
                "capability_id": capability_id,
                "version": str(spec.version),
                "input_schema": input_model.model_json_schema(),
            }
        )
    return result


def _task_intent_value(task: WorkBoardTask, *, handoff_ids: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "version": M5_DIGEST_VERSION,
        "owner_principal_id": task.owner_principal_id,
        "owner_session_id": task.owner_session_id,
        "goal_id": task.goal_id,
        "goal_revision": int(task.goal_revision or 0),
        "capability_id": str(task.capability_id or ""),
        "capability_version": _capability_version(str(task.capability_id or "")),
        "typed_input_digest": str(task.typed_input_digest or ""),
        "title": normalize_m5_memory_text(task.title or "") if task.title else "",
        "body": normalize_m5_memory_text(task.body or "") if task.body else "",
        "handoff_ids": sorted(str(item) for item in handoff_ids if str(item)),
    }


def m5_task_intent_digest(task: WorkBoardTask, *, handoff_ids: Sequence[str] = ()) -> str:
    return m5_digest(_task_intent_value(task, handoff_ids=handoff_ids))


def m5_source_context_digest(task: WorkBoardTask, *, task_intent_digest: str | None = None) -> str:
    """Digest the comparable task context without selected capability inputs.

    Capability and typed-input bindings remain in ``m5_task_intent_digest``;
    this context is deliberately shared by alternative candidates for the
    same owner, goal revision, title, and body.
    """

    return m5_digest(
        {
            "version": M5_DIGEST_VERSION,
            "owner_principal_id": task.owner_principal_id,
            "owner_session_id": task.owner_session_id,
            "goal_id": task.goal_id,
            "goal_revision": int(task.goal_revision or 0),
            "title": normalize_m5_memory_text(task.title or "") if task.title else "",
            "body": normalize_m5_memory_text(task.body or "") if task.body else "",
        }
    )


def _validated_candidates(
    task: WorkBoardTask,
    values: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    if values is None:
        values = [str(task.capability_id or "")]
    result: list[dict[str, Any]] = []
    try:
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES
    except Exception:
        REGISTERED_CAPABILITIES = {}
    for item in list(values)[:M5_MAX_CANDIDATES]:
        if isinstance(item, Mapping):
            candidate = str(item.get("capability_id") or "").strip()
            supplied_version = str(item.get("capability_version") or "").strip()
        else:
            candidate = str(item or "").strip()
            supplied_version = ""
        spec = REGISTERED_CAPABILITIES.get(candidate)
        if spec is None or (supplied_version and supplied_version != str(spec.version)):
            continue
        raw_inputs = item.get("inputs") if isinstance(item, Mapping) else None
        if raw_inputs is not None and not isinstance(raw_inputs, Mapping):
            continue
        if raw_inputs is not None:
            try:
                from src.work_board.dispatcher import _TYPED_INPUT_MODELS

                input_model = _TYPED_INPUT_MODELS.get(candidate)
                if input_model is not None:
                    input_model.model_validate(dict(raw_inputs))
            except Exception:
                continue
        typed_input_digest = (
            str(item.get("typed_input_digest") or "").strip().lower()
            if isinstance(item, Mapping)
            else str(task.typed_input_digest or "").strip().lower()
        )
        if typed_input_digest and not _DIGEST.fullmatch(typed_input_digest):
            continue
        # M2 owns the persisted typed-input digest.  A GoalCandidateRequest
        # may carry a typed input object for validation, but it cannot replace
        # the task's stored digest or authority.
        result.append(
            {
                "capability_id": candidate,
                "capability_version": str(spec.version),
                "typed_input_digest": typed_input_digest,
                "inputs": dict(raw_inputs or {}),
                "evidence_refs": [
                    str(ref).strip()
                    for ref in (item.get("evidence_refs") or [])
                    if isinstance(ref, str) and ref.strip() and _SAFE_ID.fullmatch(ref.strip())
                ][:20]
                if isinstance(item, Mapping)
                else [],
            }
        )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in result:
        key = (
            candidate["capability_id"],
            candidate["capability_version"],
            candidate["typed_input_digest"],
        )
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _goal_candidate_input_digest(candidate: Any) -> str:
    """Digest the typed candidate input without retaining its values."""

    inputs = getattr(candidate, "inputs", None)
    if inputs is None and isinstance(candidate, Mapping):
        inputs = candidate.get("inputs")
    if not isinstance(inputs, Mapping):
        inputs = {}
    return m5_digest({"version": M5_DIGEST_VERSION, "inputs": dict(inputs)})


def _validated_goal_candidates(values: Sequence[Any]) -> list[dict[str, Any]]:
    """Validate an ordered GoalCandidateRequest list against the M2 registry.

    Goal decisions do not own a board task or its persisted typed-input file,
    so this validator checks the same registered capability/version and typed
    input Pydantic model used by M2, then records a digest of the supplied
    typed values.  The returned order is the stable caller order after invalid
    entries are removed; no value from an invalid entry can become authority.
    """

    try:
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES, _TYPED_INPUT_MODELS
    except ImportError:
        return []
    result: list[dict[str, Any]] = []
    for item in list(values)[:M5_MAX_CANDIDATES]:
        try:
            if isinstance(item, Mapping):
                candidate_id = str(item.get("capability_id") or "").strip()
                requested_version = str(item.get("capability_version") or "").strip()
                inputs = item.get("inputs")
                evidence_refs = item.get("evidence_refs")
            else:
                candidate_id = str(getattr(item, "capability_id", "") or "").strip()
                requested_version = str(getattr(item, "capability_version", "") or "").strip()
                inputs = getattr(item, "inputs", None)
                evidence_refs = getattr(item, "evidence_refs", None)
            if not isinstance(inputs, Mapping):
                continue
            spec = REGISTERED_CAPABILITIES.get(candidate_id)
            if spec is None or (requested_version and requested_version != str(spec.version)):
                continue
            input_model = _TYPED_INPUT_MODELS.get(candidate_id)
            if input_model is None:
                continue
            input_model.model_validate(dict(inputs))
            safe_evidence = [
                str(ref).strip()
                for ref in (evidence_refs or [])
                if isinstance(ref, str) and ref.strip() and _SAFE_ID.fullmatch(ref.strip())
            ][:20]
        except (ValidationError, TypeError, ValueError, KeyError):
            continue
        result.append(
            {
                "capability_id": candidate_id,
                "capability_version": str(spec.version),
                "inputs": dict(inputs),
                "typed_input_digest": _goal_candidate_input_digest(item),
                "evidence_refs": safe_evidence,
            }
        )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in result:
        key = (item["capability_id"], item["capability_version"], item["typed_input_digest"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def validate_goal_candidate_requests(values: Sequence[Any]) -> list[dict[str, Any]]:
    """Public bounded validation projection used by the goal hook."""

    return _validated_goal_candidates(values)


def m5_goal_candidate_set_digest(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    goal_id: str,
    goal_revision: int,
    candidates: Sequence[Any],
) -> str:
    validated = _validated_goal_candidates(candidates)
    return m5_digest(
        {
            "version": M5_DIGEST_VERSION,
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
            "goal_id": goal_id,
            "goal_revision": int(goal_revision),
            "candidates": validated,
        }
    )


def m5_goal_source_context_digest(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    goal_id: str,
    goal_revision: int,
    task_title: str,
    task_body: str,
) -> str:
    """Derive comparable context while excluding selected capability inputs."""

    return m5_digest(
        {
            "version": M5_DIGEST_VERSION,
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
            "goal_id": goal_id,
            "goal_revision": int(goal_revision),
            "title": normalize_m5_memory_text(task_title or "") if task_title else "",
            "body": normalize_m5_memory_text(task_body or "") if task_body else "",
        }
    )


def _validated_candidate_capability_ids(
    task: WorkBoardTask,
    values: Sequence[Any] | None = None,
) -> list[str]:
    result = []
    for candidate in _validated_candidates(task, values):
        value = candidate["capability_id"]
        if value not in result:
            result.append(value)
    return result


def m5_candidate_action_ids(task: WorkBoardTask, *, candidate_capability_ids: Sequence[Any] | None = None) -> list[str]:
    result = _validated_candidate_capability_ids(task, candidate_capability_ids)
    return [f"dispatch:{item}" for item in result]


def m5_candidate_set_digest(task: WorkBoardTask, *, candidate_capability_ids: Sequence[Any] | None = None) -> str:
    return m5_digest(
        {
            "version": M5_DIGEST_VERSION,
            "candidates": _validated_candidates(task, candidate_capability_ids),
        }
    )


def m5_memory_scope(
    task: WorkBoardTask,
    *,
    source_context_digest: str,
    preferred_capability_id: str | None = None,
    candidate_capability_ids: Sequence[Any] | None = None,
) -> dict[str, Any]:
    preferred = str(preferred_capability_id or task.capability_id or "").strip()
    candidates: list[str] = []
    for item in candidate_capability_ids or [preferred]:
        if isinstance(item, Mapping):
            value = str(item.get("capability_id") or "").strip()
        else:
            value = str(item).strip()
        if value:
            candidates.append(value)
    candidates = list(dict.fromkeys(candidates))[:20]
    if preferred and preferred not in candidates:
        candidates.insert(0, preferred)
        candidates = candidates[:20]
    return {
        "schema_version": M5_SCOPE_SCHEMA_VERSION,
        "owner_principal_id": task.owner_principal_id,
        "owner_session_id": task.owner_session_id,
        "goal_id": task.goal_id,
        "goal_revision": int(task.goal_revision or 0),
        "capability_id": str(task.capability_id or ""),
        "capability_version": _capability_version(str(task.capability_id or "")),
        "typed_input_digest": str(task.typed_input_digest or ""),
        "source_context_digest": source_context_digest,
        "preferred_capability_id": preferred or None,
        "candidate_capability_ids": candidates,
    }


def _source_refs(proof: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("readback_id", "artifact_id", "verification_id", "verifier_id", "effect_id_digest"):
        value = proof.get(key)
        if isinstance(value, str) and _SAFE_ID.fullmatch(value) and value not in refs:
            refs.append(value)
    return refs[:20]


def _proof_digest(proof: Mapping[str, Any]) -> str:
    safe = {
        key: proof.get(key)
        for key in (
            "receipt_kind",
            "workflow_run_id",
            "status",
            "content_sha256",
            "readback_id",
            "artifact_id",
            "verification_id",
            "verifier_id",
            "effect_id_digest",
            "digest",
            "verified_at",
        )
        if proof.get(key) is not None
    }
    return m5_digest(safe)


def _structured_source_candidate(proof: M5SourceProof) -> str:
    """Build a bounded fact from proof identifiers, never from source prose."""

    ref = next(iter(_source_refs(proof.readback)), proof.evidence_digest)
    return sanitize_m5_memory_text(
        f"Verified {proof.task.capability_id} completed successfully for goal "
        f"{proof.task.goal_id} at revision {proof.task.goal_revision}; independent "
        f"readback {ref} is bound to the source attempt."
    )


def _receipt_input_value(
    task: WorkBoardTask,
    *,
    intent_digest: str,
    source_context_digest: str,
    action_ids: Sequence[str],
    candidate_set_digest: str = "",
    accepted_proposal_id: str = M5_NONE_PROPOSAL,
    accepted_memory_id: str = M5_NONE_MEMORY,
    accepted_memory_digest: str = M5_NONE_DIGEST,
    decision_effect: str = MemoryProposalDecisionEffect.none.value,
) -> dict[str, Any]:
    return {
        "version": M5_DIGEST_VERSION,
        "owner_principal_id": task.owner_principal_id,
        "owner_session_id": task.owner_session_id,
        "later_task_id": task.task_id,
        "later_task_revision": int(task.task_revision or 0),
        "task_intent_digest": intent_digest,
        "goal_id": task.goal_id,
        "goal_revision": int(task.goal_revision or 0),
        "capability_id": str(task.capability_id or ""),
        "capability_version": _capability_version(str(task.capability_id or "")),
        "typed_input_digest": str(task.typed_input_digest or ""),
        "source_context_digest": source_context_digest,
        "candidate_action_ids": sorted(action_ids),
        "candidate_set_digest": candidate_set_digest,
        "accepted_proposal_id": accepted_proposal_id,
        "accepted_memory_id": accepted_memory_id,
        "accepted_memory_content_digest": accepted_memory_digest,
        "decision_effect": decision_effect,
    }


def _proposal_payload(proposal: MemoryProposal, *, include_preview: bool = True) -> dict[str, Any]:
    scope = _decode_object(proposal.memory_scope_json)
    payload: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "schema_version": proposal.schema_version,
        "owner_principal_id": proposal.owner_principal_id,
        "owner_session_id": proposal.owner_session_id,
        "source_task_id": proposal.source_task_id,
        "source_task_revision": proposal.source_task_revision,
        "source_attempt_id": proposal.source_attempt_id,
        "source_attempt_fence": proposal.source_attempt_fence,
        "workflow_run_id": proposal.workflow_run_id,
        "goal_id": proposal.goal_id,
        "goal_revision": proposal.goal_revision,
        "capability_id": proposal.capability_id,
        "capability_version": proposal.capability_version,
        "typed_input_digest": proposal.typed_input_digest,
        "source_context_digest": proposal.source_context_digest,
        "evidence_digest": proposal.evidence_digest,
        "readback_kind": proposal.readback_kind,
        "readback_ref": proposal.readback_ref,
        "readback_digest": proposal.readback_digest,
        "artifact_ref": proposal.artifact_ref,
        "artifact_digest": proposal.artifact_digest,
        "status": _enum_value(proposal.status),
        "memory_kind": _enum_value(proposal.memory_kind) if proposal.memory_kind else None,
        "scope": scope if proposal.privacy_state is not MemoryProposalPrivacyState.redacted else None,
        "preview_text": proposal.preview_text if include_preview and proposal.privacy_state is not MemoryProposalPrivacyState.redacted else None,
        "preview_text_digest": proposal.preview_text_digest,
        "proposed_text": proposal.preview_text if include_preview and proposal.privacy_state is not MemoryProposalPrivacyState.redacted else None,
        "proposed_text_digest": proposal.preview_text_digest,
        "corrects_memory_id": proposal.corrects_memory_id,
        "preferred_capability_id": scope.get("preferred_capability_id"),
        "registered_capabilities": m5_registered_capability_options(),
        "evidence_refs": _decode_list(proposal.source_refs_json),
        "decision_effect": _enum_value(proposal.decision_effect),
        "allowed_decision_effects": ["none", "require_operator_confirmation"],
        "confidence": proposal.confidence,
        "reason_code": proposal.reason_code,
        "recovery_action": proposal.recovery_action,
        "rollback_reason": proposal.rollback_reason,
        "provider_contact_started": bool(proposal.provider_contact_started),
        "provider_contact_state": _enum_value(proposal.provider_contact_state),
        "provider_contact_count": int(proposal.provider_contact_count or 0),
        "privacy_state": _enum_value(proposal.privacy_state),
        "accepted_memory_id": proposal.accepted_memory_id,
        "accepted_memory_content_digest": proposal.accepted_memory_content_digest,
        "revision": proposal.revision,
        "expires_at": _utc(proposal.expires_at).isoformat() if proposal.expires_at else None,
        "created_at": _utc(proposal.created_at).isoformat() if proposal.created_at else None,
        "updated_at": _utc(proposal.updated_at).isoformat() if proposal.updated_at else None,
    }
    if proposal.privacy_state is MemoryProposalPrivacyState.redacted:
        payload.pop("source_context_digest", None)
    return payload


def _receipt_payload(receipt: WorkBoardDecisionReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "schema_version": receipt.schema_version,
        "receipt_stage": _enum_value(receipt.receipt_stage),
        "receipt_binding_digest": receipt.receipt_binding_digest,
        "owner_principal_id": receipt.owner_principal_id,
        "owner_session_id": receipt.owner_session_id,
        "source_proposal_id": receipt.source_proposal_id,
        "source_proposal_revision": receipt.source_proposal_revision,
        "source_baseline_receipt_id": receipt.source_baseline_receipt_id,
        "source_task_id": receipt.source_task_id,
        "source_attempt_id": receipt.source_attempt_id,
        "later_task_id": receipt.later_task_id,
        "later_task_revision": receipt.later_task_revision,
        "goal_id": receipt.goal_id,
        "goal_revision": receipt.goal_revision,
        "capability_id": receipt.capability_id,
        "capability_version": receipt.capability_version,
        "typed_input_digest": receipt.typed_input_digest,
        "task_intent_digest": receipt.task_intent_digest,
        "source_context_digest": receipt.source_context_digest,
        "evidence_ids": _decode_list(receipt.retrieval_evidence_ids_json),
        "accepted_memory_id": receipt.accepted_memory_id,
        "accepted_memory_content_digest": receipt.accepted_memory_content_digest,
        "before_input_digest": receipt.before_input_digest,
        "after_input_digest": receipt.after_input_digest,
        "before_action_id": receipt.before_action_id,
        "after_action_id": receipt.after_action_id,
        "before_selected_capability_id": receipt.before_selected_capability_id,
        "after_selected_capability_id": receipt.after_selected_capability_id,
        "candidate_set_digest": receipt.candidate_set_digest,
        "confirmed_action_id": receipt.confirmed_action_id,
        "decision_status": _enum_value(receipt.decision_status),
        "admission_status": _enum_value(receipt.admission_status),
        "reason": receipt.reason,
        "revision": receipt.revision,
        "created_at": _utc(receipt.created_at).isoformat() if receipt.created_at else None,
        "updated_at": _utc(receipt.updated_at).isoformat() if receipt.updated_at else None,
    }


@dataclass(frozen=True)
class M5SourceProof:
    task: WorkBoardTask
    attempt: WorkBoardAttempt
    run: WorkflowRunState
    readback: dict[str, Any]
    evidence_digest: str
    source_context_digest: str
    task_intent_digest: str
    capability_version: str


@dataclass(frozen=True)
class M5GoalDecision:
    """A content-free, provider-free comparison for the goal decision hook."""

    decision_status: WorkBoardDecisionStatus
    reason: str
    source_context_digest: str
    candidate_set_digest: str
    before_input_digest: str
    after_input_digest: str
    before_selected_capability_id: str | None
    after_selected_capability_id: str | None
    after_typed_input_digest: str | None
    accepted_proposal_id: str | None
    accepted_memory_id: str | None
    accepted_memory_content_digest: str | None
    evidence_ids: tuple[str, ...]
    receipt: WorkBoardDecisionReceipt


def _goal_receipt_task_id(goal_id: str) -> str:
    return f"goal-decision:{_safe_identifier(goal_id, field='goal_id')}"


def _goal_candidate_input_digest_value(candidate: Mapping[str, Any]) -> str:
    return m5_digest(
        {
            "version": M5_DIGEST_VERSION,
            "capability_id": candidate["capability_id"],
            "capability_version": candidate["capability_version"],
            "typed_input_digest": candidate["typed_input_digest"],
        }
    )


async def evaluate_goal_candidate_memory(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    goal_id: str,
    goal_revision: int,
    later_task_id: str,
    later_task_revision: int,
    candidates: Sequence[Any],
    baseline_capability_id: str | None = None,
) -> M5GoalDecision:
    """Compare one bounded goal candidate set with accepted M5 memory.

    This is the M5 integration point for the goal-conditioned loop.  It only
    consumes the existing canonical ``MemoryProposal``/``Memory`` rows; it
    never infers a preference from text and never registers or mutates a
    capability.  A receipt is written even when validation or retrieval
    produces no learning, so an unsuccessful source cannot disappear.
    """

    owner_principal_id = _safe_identifier(owner_principal_id, field="owner_principal_id")
    owner_session_id = _safe_identifier(owner_session_id, field="owner_session_id")
    goal_id = _safe_identifier(goal_id, field="goal_id")
    if int(goal_revision) < 1:
        raise ValueError("goal_revision_invalid")
    validated = _validated_goal_candidates(candidates)
    candidate_set_digest = m5_goal_candidate_set_digest(
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        goal_id=goal_id,
        goal_revision=int(goal_revision),
        candidates=candidates,
    )
    later_task_id = _safe_identifier(later_task_id, field="later_task_id")
    if int(later_task_revision) < 1:
        raise ValueError("later_task_revision_invalid")
    baseline = next(
        (
            item
            for item in validated
            if baseline_capability_id is not None
            and item["capability_id"] == str(baseline_capability_id)
        ),
        (validated[0] if validated and baseline_capability_id is None else None),
    )
    before_id = baseline["capability_id"] if baseline else None
    before_input = m5_digest(
        {
            "version": M5_DIGEST_VERSION,
            "candidate_set_digest": candidate_set_digest,
            "selected_capability_id": before_id,
            "selected_input_digest": _goal_candidate_input_digest_value(baseline) if baseline else M5_NONE_DIGEST,
        }
    )
    status = WorkBoardDecisionStatus.no_change
    reason = "no_matching_accepted_memory"
    after_id = before_id
    selected = baseline
    accepted_proposal_id: str | None = None
    accepted_memory_id: str | None = None
    accepted_memory_digest: str | None = None
    evidence_ids: tuple[str, ...] = ()
    eligible: list[tuple[MemoryProposal, Memory]] = []
    source_baseline: WorkBoardDecisionReceipt | None = None
    context = ""

    async with get_session() as db:
        from src.work_board.repository import _begin_sqlite_immediate

        await _begin_sqlite_immediate(db)
        task = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == later_task_id,
                    WorkBoardTask.owner_principal_id == owner_principal_id,
                    WorkBoardTask.owner_session_id == owner_session_id,
                )
            )
        ).scalar_one_or_none()
        if task is None:
            raise PermissionError("task_owner_session_forbidden")
        if task.goal_id != goal_id or task.goal_revision != int(goal_revision):
            raise ValueError("stale_task_goal_revision")
        if task.task_revision != int(later_task_revision):
            raise ValueError("stale_task_revision")
        if _enum_value(task.status) not in {WorkBoardStatus.todo.value, WorkBoardStatus.ready.value}:
            raise ValueError("candidate_set_task_phase_invalid")
        goal = (
            await db.execute(
                select(Goal).where(
                    Goal.id == goal_id,
                    Goal.owner_principal_id == owner_principal_id,
                    Goal.owner_session_id == owner_session_id,
                )
            )
        ).scalar_one_or_none()
        if goal is None:
            raise PermissionError("goal_owner_session_forbidden")
        if int(goal.revision or 0) != int(goal_revision):
            raise ValueError("stale_goal_revision")
        context = m5_goal_source_context_digest(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            goal_id=goal_id,
            goal_revision=int(goal_revision),
            task_title=task.title,
            task_body=task.body,
        )
        if not _DIGEST.fullmatch(str(context).lower()):
            raise ValueError("source_context_digest_invalid")

        if not validated or baseline is None:
            status = WorkBoardDecisionStatus.blocked
            reason = "no_dispatchable_goal_candidates" if validated else "no_valid_goal_candidates"
        else:
            eligible = await memory_repository.list_m5_accepted_memory_candidates(
                db,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                goal_id=goal_id,
                goal_revision=int(goal_revision),
                source_context_digest=str(context).lower(),
                limit=M5_DISCOVERY_LIMIT + 1,
            )
            eligible = [
                (proposal, memory)
                for proposal, memory in eligible
                if (
                    _decode_object(proposal.memory_scope_json).get("source_context_digest")
                    == str(context).lower()
                    and _decode_object(proposal.memory_scope_json).get("goal_id") == goal_id
                    and int(_decode_object(proposal.memory_scope_json).get("goal_revision") or 0)
                    == int(goal_revision)
                )
            ]
            binding_block = (
                await db.execute(
                    select(MemoryProposal)
                    .where(
                        MemoryProposal.owner_principal_id == owner_principal_id,
                        MemoryProposal.owner_session_id == owner_session_id,
                        MemoryProposal.goal_id == goal_id,
                        MemoryProposal.goal_revision == int(goal_revision),
                        MemoryProposal.source_context_digest == str(context).lower(),
                        MemoryProposal.status == MemoryProposalStatus.blocked,
                        MemoryProposal.reason_code.in_(
                            [
                                "accepted_memory_binding_unverifiable",
                                "accepted_memory_binding_mismatch",
                            ]
                        ),
                    )
                    .order_by(MemoryProposal.updated_at.desc(), MemoryProposal.proposal_id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if binding_block is not None:
                status = WorkBoardDecisionStatus.blocked
                reason = binding_block.reason_code
            elif len(eligible) > 1:
                status = WorkBoardDecisionStatus.blocked
                reason = "ambiguous_accepted_memory"
            elif eligible:
                proposal, memory = eligible[0]
                scope = _decode_object(proposal.memory_scope_json)
                preferred = scope.get("preferred_capability_id")
                candidate_ids = {item["capability_id"] for item in validated}
                accepted_proposal_id = proposal.proposal_id
                accepted_memory_id = memory.id
                accepted_memory_digest = proposal.accepted_memory_content_digest
                raw_evidence = _decode_list(proposal.source_refs_json)
                evidence_ids = tuple(
                    str(item) for item in raw_evidence if isinstance(item, str) and _SAFE_ID.fullmatch(item)
                )[:20]
                source_baseline = (
                    await db.execute(
                        select(WorkBoardDecisionReceipt).where(
                            WorkBoardDecisionReceipt.receipt_stage == WorkBoardDecisionReceiptStage.source_baseline,
                            WorkBoardDecisionReceipt.owner_principal_id == owner_principal_id,
                            WorkBoardDecisionReceipt.owner_session_id == owner_session_id,
                            WorkBoardDecisionReceipt.source_proposal_id == proposal.proposal_id,
                            WorkBoardDecisionReceipt.source_task_id == proposal.source_task_id,
                            WorkBoardDecisionReceipt.source_attempt_id == proposal.source_attempt_id,
                            WorkBoardDecisionReceipt.goal_id == goal_id,
                            WorkBoardDecisionReceipt.goal_revision == int(goal_revision),
                            WorkBoardDecisionReceipt.source_context_digest == context,
                        )
                    )
                ).scalar_one_or_none()
                if source_baseline is None:
                    status = WorkBoardDecisionStatus.no_comparable
                    reason = "source_baseline_missing"
                elif not _m5_receipt_integrity_matches(source_baseline):
                    status = WorkBoardDecisionStatus.blocked
                    reason = "source_baseline_integrity_unverifiable"
                elif not _m5_receipt_binding_matches(source_baseline, proposal):
                    status = WorkBoardDecisionStatus.blocked
                    reason = "source_baseline_binding_mismatch"
                elif proposal.decision_effect is not MemoryProposalDecisionEffect.require_operator_confirmation:
                    reason = "accepted_memory_effect_none"
                elif not isinstance(preferred, str) or preferred not in candidate_ids:
                    status = WorkBoardDecisionStatus.no_comparable
                    reason = "preferred_capability_not_current_candidate"
                else:
                    selected = next(item for item in validated if item["capability_id"] == preferred)
                    after_id = preferred
                    before_id = str(source_baseline.before_selected_capability_id or "") or None
                    before_input = source_baseline.before_input_digest
                    if (
                        preferred != before_id
                        or selected["typed_input_digest"] != str(proposal.typed_input_digest or "")
                    ):
                        status = WorkBoardDecisionStatus.changed
                        reason = "accepted_memory_selected"
                    else:
                        reason = "accepted_memory_same_baseline"

        if source_baseline is not None and status is not WorkBoardDecisionStatus.blocked:
            before_id = str(source_baseline.before_selected_capability_id or "") or None
            before_input = source_baseline.before_input_digest

        after_input = m5_digest(
            {
                "version": M5_DIGEST_VERSION,
                "candidate_set_digest": candidate_set_digest,
                "selected_capability_id": after_id,
                "selected_input_digest": _goal_candidate_input_digest_value(selected) if selected else M5_NONE_DIGEST,
                "accepted_memory_content_digest": accepted_memory_digest or M5_NONE_DIGEST,
                "source_baseline_receipt_id": source_baseline.receipt_id if source_baseline else None,
            }
        )
        binding = m5_digest(
            {
                "version": M5_RECEIPT_SCHEMA_VERSION,
                "stage": WorkBoardDecisionReceiptStage.later_comparison.value,
                "owner_principal_id": owner_principal_id,
                "owner_session_id": owner_session_id,
                "later_task_id": later_task_id,
                "later_task_revision": int(later_task_revision),
                "goal_id": goal_id,
                "goal_revision": int(goal_revision),
                "source_context_digest": str(context).lower(),
                "candidate_set_digest": candidate_set_digest,
                "accepted_proposal_id": accepted_proposal_id or M5_NONE_PROPOSAL,
                "accepted_memory_id": accepted_memory_id or M5_NONE_MEMORY,
                "accepted_memory_content_digest": accepted_memory_digest or M5_NONE_DIGEST,
            }
        )
        receipt = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_binding_digest == binding
                )
            )
        ).scalar_one_or_none()
        if receipt is None:
            receipt = WorkBoardDecisionReceipt(
                receipt_stage=WorkBoardDecisionReceiptStage.later_comparison,
                receipt_binding_digest=binding,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                source_proposal_id=accepted_proposal_id,
                source_proposal_revision=(eligible[0][0].revision if eligible else 0),
                source_task_id=(eligible[0][0].source_task_id if eligible else None),
                source_task_revision=(eligible[0][0].source_task_revision if eligible else 0),
                source_attempt_id=(eligible[0][0].source_attempt_id if eligible else None),
                source_attempt_fence=(eligible[0][0].source_attempt_fence if eligible else 0),
                source_workflow_run_id=(eligible[0][0].workflow_run_id if eligible else None),
                source_workflow_run_revision=(eligible[0][0].workflow_run_revision if eligible else 0),
                later_task_id=later_task_id,
                later_task_revision=int(later_task_revision),
                goal_id=goal_id,
                goal_revision=int(goal_revision),
                capability_id=after_id or "",
                task_intent_digest=m5_digest(
                    {
                        "owner_principal_id": owner_principal_id,
                        "owner_session_id": owner_session_id,
                        "goal_id": goal_id,
                        "goal_revision": int(goal_revision),
                        "candidate_set_digest": candidate_set_digest,
                    }
                ),
                source_context_digest=str(context).lower(),
                candidate_set_digest=candidate_set_digest,
                accepted_memory_id=accepted_memory_id,
                accepted_memory_content_digest=accepted_memory_digest,
                before_input_digest=before_input,
                after_input_digest=after_input,
                before_action_id=f"dispatch:{before_id}" if before_id else M5_NONE_ACTION,
                after_action_id=f"dispatch:{after_id}" if after_id else M5_NONE_ACTION,
                before_selected_capability_id=before_id,
                after_selected_capability_id=after_id,
                comparison_context_digest=str(context).lower(),
                retrieval_evidence_ids_json=json.dumps(evidence_ids, separators=(",", ":")),
                decision_status=status,
                admission_status=WorkBoardDecisionAdmissionStatus.not_required,
                reason=reason,
                source_baseline_receipt_id=source_baseline.receipt_id if source_baseline else None,
                capability_version=selected["capability_version"] if selected else "",
                typed_input_digest=selected["typed_input_digest"] if selected else "",
            )
            receipt.receipt_integrity_mac = _m5_receipt_integrity_mac(receipt)
            db.add(receipt)
            await db.flush()

        return M5GoalDecision(
            decision_status=status,
            reason=reason,
            source_context_digest=str(context).lower(),
            candidate_set_digest=candidate_set_digest,
            before_input_digest=before_input,
            after_input_digest=after_input,
            before_selected_capability_id=before_id,
            after_selected_capability_id=after_id,
            after_typed_input_digest=(selected["typed_input_digest"] if selected else None),
            accepted_proposal_id=accepted_proposal_id,
            accepted_memory_id=accepted_memory_id,
            accepted_memory_content_digest=accepted_memory_digest,
            evidence_ids=evidence_ids,
            receipt=receipt,
        )


async def _verified_source(
    db: AsyncSession,
    task: WorkBoardTask,
    *,
    requested_attempt_id: str | None = None,
) -> M5SourceProof:
    if task.status is not WorkBoardStatus.done:
        raise ValueError("source_not_verified")
    from src.work_board import review as review_service

    statement = select(WorkBoardAttempt).where(
        WorkBoardAttempt.task_id == task.task_id,
        WorkBoardAttempt.ended_at.is_not(None),
    ).order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1)
    attempt = (await db.execute(statement)).scalar_one_or_none()
    if attempt is None or (
        requested_attempt_id is not None and attempt.attempt_id != requested_attempt_id
    ):
        raise ValueError("stale_source_attempt")
    proof = await review_service._verified_workflow_readback(db, task, attempt)
    if proof is None:
        raise ValueError("source_not_verified")
    run = (
        await db.execute(
            select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise ValueError("source_not_verified")
    intent_digest = m5_task_intent_digest(task)
    context_digest = m5_source_context_digest(task, task_intent_digest=intent_digest)
    evidence_digest = m5_digest(
        {
            "version": M5_DIGEST_VERSION,
            "owner_principal_id": task.owner_principal_id,
            "owner_session_id": task.owner_session_id,
            "source_task_id": task.task_id,
            "source_task_revision": int(task.task_revision or 0),
            "source_attempt_id": attempt.attempt_id,
            "source_attempt_fence": int(attempt.fencing_token or 0),
            "workflow_run_id": attempt.workflow_run_id,
            "goal_id": task.goal_id,
            "goal_revision": int(task.goal_revision or 0),
            "capability_id": str(task.capability_id or ""),
            "capability_version": _capability_version(str(task.capability_id or "")),
            "typed_input_digest": str(task.typed_input_digest or ""),
            "readback": {"refs": _source_refs(proof), "digest": _proof_digest(proof)},
        }
    )
    return M5SourceProof(
        task=task,
        attempt=attempt,
        run=run,
        readback=dict(proof),
        evidence_digest=evidence_digest,
        source_context_digest=context_digest,
        task_intent_digest=intent_digest,
        capability_version=_capability_version(str(task.capability_id or "")),
    )


async def _validate_current_proposal_source(
    db: AsyncSession,
    proposal: MemoryProposal,
    *,
    owner_principal_id: str,
    owner_session_id: str,
    expected_task_revision: int,
    expected_goal_revision: int,
) -> None:
    """Re-read every authority and evidence binding before canonical accept."""

    task = (
        await db.execute(
            select(WorkBoardTask).where(
                WorkBoardTask.task_id == proposal.source_task_id,
                WorkBoardTask.owner_principal_id == owner_principal_id,
                WorkBoardTask.owner_session_id == owner_session_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise PermissionError("task_owner_session_forbidden")
    if (
        task.task_revision != int(expected_task_revision)
        or task.task_revision != int(proposal.source_task_revision)
    ):
        raise ValueError("stale_task_revision")
    if task.status is not WorkBoardStatus.done:
        raise ValueError("stale_source_task_status")
    goal = (
        await db.execute(
            select(Goal).where(
                Goal.id == proposal.goal_id,
                Goal.owner_principal_id == owner_principal_id,
                Goal.owner_session_id == owner_session_id,
            )
        )
    ).scalar_one_or_none()
    if goal is None:
        raise PermissionError("goal_owner_session_forbidden")
    if (
        goal.revision != int(expected_goal_revision)
        or goal.revision != int(proposal.goal_revision)
        or task.goal_id != proposal.goal_id
        or task.goal_revision != proposal.goal_revision
    ):
        raise ValueError("stale_goal_revision")
    proof = await _verified_source(
        db,
        task,
        requested_attempt_id=proposal.source_attempt_id,
    )
    if (
        proof.attempt.fencing_token != proposal.source_attempt_fence
        or proof.attempt.workflow_run_id != proposal.workflow_run_id
        or proof.evidence_digest != proposal.evidence_digest
        or _proof_digest(proof.readback) != proposal.readback_digest
        or int(getattr(proof.run, "revision", 0) or 0) != proposal.workflow_run_revision
    ):
        raise ValueError("stale_source_evidence")


async def _write_memory_action_audit(
    db: AsyncSession,
    *,
    owner_principal_id: str,
    owner_session_id: str,
    proposal: MemoryProposal,
    action: str,
) -> AuditEvent:
    """Keep operator review actions in the same canonical transaction."""

    session = await db.get(Session, owner_session_id)
    if session is None:
        session = Session(id=owner_session_id, owner_principal_id=owner_principal_id)
        db.add(session)
        await db.flush()
    elif session.owner_principal_id not in {None, owner_principal_id}:
        raise PermissionError("session_owner_mismatch")
    memory_id = proposal.accepted_memory_id
    event_type = {
        "accept": "memory_corrected" if proposal.corrects_memory_id else "memory_learning_accepted",
        "edit_accept": "memory_corrected" if proposal.corrects_memory_id else "memory_learning_accepted",
        "reject": "memory_learning_rejected",
        "expire": "memory_learning_expired",
        "rollback": "memory_learning_rolled_back",
    }[action]
    event = AuditEvent(
        session_id=owner_session_id,
        actor=owner_principal_id,
        event_type=event_type,
        tool_name="memory_control",
        risk_level="low",
        policy_mode="operator_controlled",
        summary="Operator reviewed verified work-board memory",
        details_json=m5_canonical_json(
            {
                "proposal_id": proposal.proposal_id,
                "source_task_id": proposal.source_task_id,
                "source_attempt_id": proposal.source_attempt_id,
                "accepted_memory_id": memory_id,
                "corrects_memory_id": proposal.corrects_memory_id,
                "action": action,
                **(
                    {"rollback_reason": proposal.rollback_reason}
                    if action == "rollback"
                    else {}
                ),
            }
        ),
    )
    db.add(event)
    await db.flush()
    return event


async def _write_source_baseline(db: AsyncSession, proof: M5SourceProof, proposal: MemoryProposal) -> WorkBoardDecisionReceipt:
    action_id = f"dispatch:{proof.task.capability_id}"
    before = m5_digest(
        _receipt_input_value(
            proof.task,
            intent_digest=proof.task_intent_digest,
            source_context_digest=proof.source_context_digest,
            action_ids=[action_id],
        )
    )
    binding = m5_digest(
        {
            "version": M5_RECEIPT_SCHEMA_VERSION,
            "stage": WorkBoardDecisionReceiptStage.source_baseline.value,
            "owner_principal_id": proof.task.owner_principal_id,
            "owner_session_id": proof.task.owner_session_id,
            "source_attempt_id": proof.attempt.attempt_id,
            "source_task_revision": int(proof.task.task_revision or 0),
            "source_proposal_id": proposal.proposal_id,
        }
    )
    existing = (
        await db.execute(select(WorkBoardDecisionReceipt).where(WorkBoardDecisionReceipt.receipt_binding_digest == binding))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    receipt = WorkBoardDecisionReceipt(
        receipt_stage=WorkBoardDecisionReceiptStage.source_baseline,
        receipt_binding_digest=binding,
        owner_principal_id=proof.task.owner_principal_id,
        owner_session_id=proof.task.owner_session_id,
        source_proposal_id=proposal.proposal_id,
        source_proposal_revision=proposal.revision,
        source_task_id=proof.task.task_id,
        source_task_revision=proof.task.task_revision,
        source_attempt_id=proof.attempt.attempt_id,
        source_attempt_fence=int(proof.attempt.fencing_token or 0),
        source_workflow_run_id=proof.attempt.workflow_run_id,
        source_workflow_run_revision=int(getattr(proof.run, "revision", 0) or 0),
        later_task_id=proof.task.task_id,
        later_task_revision=proof.task.task_revision,
        goal_id=proof.task.goal_id,
        goal_revision=proof.task.goal_revision,
        capability_id=str(proof.task.capability_id or ""),
        capability_version=proof.capability_version,
        typed_input_digest=str(proof.task.typed_input_digest or ""),
        task_intent_digest=proof.task_intent_digest,
        source_context_digest=proof.source_context_digest,
        before_input_digest=before,
        after_input_digest=before,
        before_action_id=action_id,
        after_action_id=action_id,
        before_selected_capability_id=str(proof.task.capability_id or "") or None,
        after_selected_capability_id=str(proof.task.capability_id or "") or None,
        comparison_context_digest=proof.source_context_digest,
        retrieval_evidence_ids_json=json.dumps(_source_refs(proof.readback), separators=(",", ":")),
        decision_status=WorkBoardDecisionStatus.no_change,
        admission_status=WorkBoardDecisionAdmissionStatus.not_required,
        reason="source_baseline",
    )
    receipt.receipt_integrity_mac = _m5_receipt_integrity_mac(receipt)
    db.add(receipt)
    await db.flush()
    return receipt


async def _write_source_failure_proposal(
    db: AsyncSession,
    *,
    task: WorkBoardTask,
    attempt_id: str,
    status: MemoryProposalStatus,
    reason: str,
    recovery_action: str,
) -> dict[str, Any]:
    """Persist a bounded source failure instead of dropping the attempt."""

    attempt = (
        await db.execute(
            select(WorkBoardAttempt).where(
                WorkBoardAttempt.task_id == task.task_id,
            ).order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None or attempt.attempt_id != attempt_id:
        raise ValueError("source_attempt_not_found")
    if int(attempt.fencing_token or 0) < 1 or not attempt.workflow_run_id:
        raise ValueError("source_attempt_binding_invalid")
    run = (
        await db.execute(
            select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise ValueError("source_workflow_run_not_found")
    if reason == "source_not_verified":
        if str(run.status or "") in {"unknown_external_effect", "cost_liability"}:
            reason = "unknown_effect"
            recovery_action = "reconcile_effect_before_retry"
        elif str(run.status or "") == "failed":
            reason = "failed_attempt_no_learning"
        else:
            reason = "readback_not_verified"
    source_fence = int(attempt.fencing_token or 0)
    workflow_run_id = str(attempt.workflow_run_id or "")
    context = m5_source_context_digest(task)
    intent = m5_task_intent_digest(task)
    evidence_digest = m5_digest(
        {
            "version": M5_SCHEMA_VERSION,
            "status": status.value,
            "reason": reason,
            "owner_principal_id": task.owner_principal_id,
            "owner_session_id": task.owner_session_id,
            "task_id": task.task_id,
            "attempt_id": attempt_id,
            "source_fence": source_fence,
        }
    )
    binding = m5_digest(
        {
            "version": M5_SCHEMA_VERSION,
            "owner_principal_id": task.owner_principal_id,
            "owner_session_id": task.owner_session_id,
            "source_task_id": task.task_id,
            "source_task_revision": task.task_revision,
            "source_attempt_id": attempt_id,
            "source_attempt_fence": source_fence,
            "workflow_run_id": workflow_run_id,
            "goal_id": task.goal_id,
            "goal_revision": task.goal_revision,
            "source_context_digest": context,
            "evidence_digest": evidence_digest,
            "status": status.value,
            "reason": reason,
        }
    )
    existing = (
        await db.execute(
            select(MemoryProposal).where(
                MemoryProposal.owner_principal_id == task.owner_principal_id,
                MemoryProposal.owner_session_id == task.owner_session_id,
                MemoryProposal.source_task_id == task.task_id,
                MemoryProposal.source_attempt_id == attempt_id,
                MemoryProposal.status == status,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_binding_digest != binding:
            raise ValueError("proposal_source_binding_conflict")
        return _proposal_payload(existing)
    proposal = MemoryProposal(
        schema_version=M5_SCHEMA_VERSION,
        owner_principal_id=task.owner_principal_id,
        owner_session_id=task.owner_session_id,
        source_task_id=task.task_id,
        source_task_revision=task.task_revision,
        source_attempt_id=attempt_id,
        source_attempt_fence=source_fence,
        workflow_run_id=workflow_run_id,
        workflow_run_revision=int(getattr(run, "revision", 0) or 0),
        goal_id=task.goal_id,
        goal_revision=task.goal_revision,
        capability_id=str(task.capability_id or ""),
        capability_version=_capability_version(str(task.capability_id or "")),
        typed_input_digest=str(task.typed_input_digest or ""),
        source_context_digest=context,
        evidence_digest=evidence_digest,
        readback_kind="none",
        readback_ref=None,
        readback_digest=m5_digest({"status": status.value, "reason": reason}),
        proposal_job_id=None,
        request_idempotency_key=binding,
        request_binding_digest=binding,
        memory_kind=None,
        memory_scope_json=None,
        preview_text=None,
        preview_text_digest=None,
        decision_effect=MemoryProposalDecisionEffect.none,
        confidence=None,
        provenance_json="{}",
        source_refs_json="[]",
        reason_code=reason,
        recovery_action=recovery_action,
        provider_contact_started=False,
        provider_contact_state=MemoryProposalProviderContactState.not_started,
        provider_contact_count=0,
        privacy_state=MemoryProposalPrivacyState.visible,
        status=status,
    )
    db.add(proposal)
    await db.flush()
    receipt_binding = m5_digest(
        {
            "version": M5_RECEIPT_SCHEMA_VERSION,
            "stage": WorkBoardDecisionReceiptStage.source_baseline.value,
            "owner_principal_id": task.owner_principal_id,
            "owner_session_id": task.owner_session_id,
            "source_task_id": task.task_id,
            "source_task_revision": task.task_revision,
            "source_attempt_id": attempt_id,
            "source_proposal_id": proposal.proposal_id,
            "status": status.value,
            "reason": reason,
        }
    )
    receipt = WorkBoardDecisionReceipt(
        receipt_stage=WorkBoardDecisionReceiptStage.source_baseline,
        receipt_binding_digest=receipt_binding,
        owner_principal_id=task.owner_principal_id,
        owner_session_id=task.owner_session_id,
        source_proposal_id=proposal.proposal_id,
        source_proposal_revision=proposal.revision,
        source_task_id=task.task_id,
        source_task_revision=task.task_revision,
        source_attempt_id=attempt_id,
        source_attempt_fence=source_fence,
        source_workflow_run_id=workflow_run_id or None,
        later_task_id=task.task_id,
        later_task_revision=task.task_revision,
        goal_id=task.goal_id,
        goal_revision=task.goal_revision,
        capability_id=str(task.capability_id or ""),
        capability_version=_capability_version(str(task.capability_id or "")),
        typed_input_digest=str(task.typed_input_digest or ""),
        task_intent_digest=intent,
        source_context_digest=context,
        candidate_set_digest=m5_digest({"capability_id": task.capability_id}),
        before_input_digest=intent,
        after_input_digest=intent,
        before_action_id=f"dispatch:{task.capability_id}" if task.capability_id else M5_NONE_ACTION,
        after_action_id=f"dispatch:{task.capability_id}" if task.capability_id else M5_NONE_ACTION,
        comparison_context_digest=context,
        retrieval_evidence_ids_json="[]",
        decision_status=(
            WorkBoardDecisionStatus.blocked
            if status is MemoryProposalStatus.blocked
            else WorkBoardDecisionStatus.no_change
        ),
        admission_status=(
            WorkBoardDecisionAdmissionStatus.blocked
            if status is MemoryProposalStatus.blocked
            else WorkBoardDecisionAdmissionStatus.not_required
        ),
        reason=reason,
    )
    receipt.receipt_integrity_mac = _m5_receipt_integrity_mac(receipt)
    db.add(receipt)
    await db.flush()
    return _proposal_payload(proposal)


async def create_memory_proposal(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    task_id: str,
    expected_task_revision: int,
    attempt_id: str,
    candidate_text: str | None = None,
    candidate_kind: MemoryKind | str | None = None,
    preferred_capability_id: str | None = None,
    decision_effect: MemoryProposalDecisionEffect | str = MemoryProposalDecisionEffect.none,
) -> dict[str, Any]:
    """Create or replay one source verified proposal.

    The public route creates a deterministic, provider-free candidate from
    safe structured proof identifiers.  No model route is contacted.  The
    optional candidate argument is reserved for a governed test adapter and
    is sanitized by the same canonical path.
    """

    owner_principal_id = _safe_identifier(owner_principal_id, field="owner_principal_id")
    owner_session_id = _safe_identifier(owner_session_id, field="owner_session_id")
    task_id = _safe_identifier(task_id, field="task_id")
    attempt_id = _safe_identifier(attempt_id, field="attempt_id")
    async with get_session() as db:
        from src.work_board.repository import _begin_sqlite_immediate

        await _begin_sqlite_immediate(db)
        task = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == task_id,
                    WorkBoardTask.owner_principal_id == owner_principal_id,
                    WorkBoardTask.owner_session_id == owner_session_id,
                )
            )
        ).scalar_one_or_none()
        if task is None:
            raise PermissionError("task owner/session binding is invalid")
        if int(task.task_revision or 0) != int(expected_task_revision):
            raise ValueError("stale_task_revision")
        goal = (
            await db.execute(select(Goal).where(Goal.id == task.goal_id))
        ).scalar_one_or_none()
        if goal is None or int(goal.revision or 0) != int(task.goal_revision or 0):
            return await _write_source_failure_proposal(
                db,
                task=task,
                attempt_id=attempt_id,
                status=MemoryProposalStatus.blocked,
                reason="stale_goal_revision",
                recovery_action="refresh_goal_and_request_new_verified_source",
            )
        try:
            proof = await _verified_source(db, task, requested_attempt_id=attempt_id)
        except ValueError as exc:
            if str(exc) == "source_not_verified":
                return await _write_source_failure_proposal(
                    db,
                    task=task,
                    attempt_id=attempt_id,
                    status=MemoryProposalStatus.no_learning,
                    reason="source_not_verified",
                    recovery_action="complete_verified_readback_then_request_again",
                )
            raise
        candidate_text = candidate_text if candidate_text is not None else _structured_source_candidate(proof)
        redaction_unavailable = False
        try:
            candidate_text = await sanitize_m5_memory_text_async(candidate_text)
            memory_kind = MemoryKind(candidate_kind or MemoryKind.fact)
            if memory_kind not in {MemoryKind.fact, MemoryKind.pattern}:
                raise ValueError("memory_kind must be fact or pattern")
            preview_digest = m5_text_digest(candidate_text)
        except (TypeError, ValueError) as exc:
            redaction_unavailable = "redaction is unavailable" in str(exc).lower()
            candidate_text = None
            memory_kind = None
            preview_digest = None
        try:
            requested_effect = MemoryProposalDecisionEffect(decision_effect)
            invalid_effect = False
        except (TypeError, ValueError):
            requested_effect = MemoryProposalDecisionEffect.none
            invalid_effect = True
            candidate_text = None
            memory_kind = None
            preview_digest = None
        derived_idempotency = m5_digest(
            {
                "version": M5_SCHEMA_VERSION,
                "owner_principal_id": owner_principal_id,
                "owner_session_id": owner_session_id,
                "source_task_id": task.task_id,
                "source_attempt_id": proof.attempt.attempt_id,
                "evidence_digest": proof.evidence_digest,
            }
        )
        existing_statement = select(MemoryProposal).where(
            MemoryProposal.owner_principal_id == owner_principal_id,
            MemoryProposal.owner_session_id == owner_session_id,
            MemoryProposal.source_task_id == task.task_id,
            MemoryProposal.source_attempt_id == proof.attempt.attempt_id,
        ).order_by(MemoryProposal.created_at.desc(), MemoryProposal.proposal_id.desc()).limit(10)
        existing_rows = list((await db.execute(existing_statement)).scalars().all())
        request_binding = m5_digest(
            {
                "version": M5_SCHEMA_VERSION,
                "owner_principal_id": owner_principal_id,
                "owner_session_id": owner_session_id,
                "source_task_id": task.task_id,
                "source_task_revision": task.task_revision,
                "source_attempt_id": proof.attempt.attempt_id,
                "source_attempt_fence": int(proof.attempt.fencing_token or 0),
                "workflow_run_id": proof.attempt.workflow_run_id,
                "goal_id": task.goal_id,
                "goal_revision": task.goal_revision,
                "capability_id": task.capability_id,
                "capability_version": proof.capability_version,
                "typed_input_digest": task.typed_input_digest or "",
                "source_context_digest": proof.source_context_digest,
                "readback_digest": _proof_digest(proof.readback),
                "idempotency_key": derived_idempotency,
            }
        )
        if existing_rows:
            existing = next(
                (item for item in existing_rows if item.request_binding_digest == request_binding),
                None,
            )
            if existing is None:
                raise ValueError("proposal_source_binding_conflict")
            return _proposal_payload(existing)
        if candidate_text is None or preview_digest is None:
            status = (
                MemoryProposalStatus.blocked
                if redaction_unavailable
                else MemoryProposalStatus.no_learning
            )
            memory_kind = None
            scope_json = None
            preview = None
            preview_digest = None
            confidence = None
            effect = MemoryProposalDecisionEffect.none
            reason = (
                "memory_redaction_unavailable"
                if redaction_unavailable
                else ("decision_effect_invalid" if invalid_effect else "memory_candidate_rejected")
            )
            recovery_action = "restore_redaction_service_then_request_again" if redaction_unavailable else "none"
            job_id = None
        else:
            preview = candidate_text
            effect = requested_effect
            scope = m5_memory_scope(
                task,
                source_context_digest=proof.source_context_digest,
                preferred_capability_id=preferred_capability_id,
            )
            scope_json = m5_canonical_json(scope)
            confidence = 0.5
            status = MemoryProposalStatus.proposed
            reason = "verified_source"
            recovery_action = "none"
            job_id = f"work-board-proposal:{derived_idempotency}"
        proposal = MemoryProposal(
            schema_version=M5_SCHEMA_VERSION,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            source_task_id=task.task_id,
            source_task_revision=task.task_revision,
            source_attempt_id=proof.attempt.attempt_id,
            source_attempt_fence=int(proof.attempt.fencing_token or 0),
            workflow_run_id=proof.attempt.workflow_run_id or "",
            workflow_run_revision=int(getattr(proof.run, "revision", 0) or 0),
            goal_id=task.goal_id,
            goal_revision=task.goal_revision,
            capability_id=str(task.capability_id or ""),
            capability_version=proof.capability_version,
            typed_input_digest=str(task.typed_input_digest or ""),
            source_context_digest=proof.source_context_digest,
            evidence_digest=proof.evidence_digest,
            readback_kind=str(proof.readback.get("kind") or "verified_workflow_readback"),
            readback_ref=next(iter(_source_refs(proof.readback)), None),
            readback_digest=_proof_digest(proof.readback),
            artifact_ref=(
                str(proof.readback.get("artifact_id") or proof.readback.get("readback_id") or "") or None
            ),
            artifact_digest=(str(proof.readback.get("content_sha256") or "") or None),
            proposal_job_id=job_id,
            request_idempotency_key=derived_idempotency,
            request_binding_digest=request_binding,
            memory_kind=memory_kind,
            memory_scope_json=scope_json,
            preview_text=preview,
            preview_text_digest=preview_digest,
            decision_effect=effect,
            confidence=confidence,
            provenance_json=m5_canonical_json(
                {
                    "schema_version": M5_PROVENANCE_SCHEMA_VERSION,
                    "owner_principal_id": owner_principal_id,
                    "owner_session_id": owner_session_id,
                    "source_task_id": task.task_id,
                    "source_attempt_id": proof.attempt.attempt_id,
                    "workflow_run_id": proof.attempt.workflow_run_id,
                    "source_context_digest": proof.source_context_digest,
                    "evidence_digest": proof.evidence_digest,
                    "readback_digest": _proof_digest(proof.readback),
                    "artifact_ref": (
                        str(proof.readback.get("artifact_id") or proof.readback.get("readback_id") or "") or None
                    ),
                    "artifact_digest": str(proof.readback.get("content_sha256") or "") or None,
                }
            ),
            source_refs_json=json.dumps(_source_refs(proof.readback), separators=(",", ":")),
            reason_code=reason,
            recovery_action=recovery_action if candidate_text is None else "none",
            provider_contact_started=False,
            provider_contact_state=MemoryProposalProviderContactState.not_started,
            provider_contact_count=0,
            privacy_state=MemoryProposalPrivacyState.visible,
            status=status,
            expires_at=_now() + timedelta(minutes=15) if status is MemoryProposalStatus.proposed else None,
        )
        if status is MemoryProposalStatus.proposed:
            proposal_job = (
                await db.execute(
                    select(WorkBoardProposal).where(
                        WorkBoardProposal.admission_job_id == job_id,
                    )
                )
            ).scalar_one_or_none()
            if proposal_job is None:
                proposal_job = WorkBoardProposal(
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    parent_task_id=task.task_id,
                    parent_revision=task.task_revision,
                    goal_revision=task.goal_revision,
                    kind="memory",
                    idempotency_key=derived_idempotency,
                    request_digest=request_binding,
                    capability_id="memory_proposal",
                    capability_version=M5_SCHEMA_VERSION,
                    authority_digest=m5_digest({"owner": owner_principal_id, "session": owner_session_id}),
                    grant_revision=task.goal_revision,
                    input_digest=str(task.typed_input_digest or ""),
                    route_id="provider-free-verified-readback",
                    admission_job_id=job_id,
                    effect_id_digest=m5_digest({"job_id": job_id})[:16],
                    provider_contact_started=False,
                    provider_contact_state="not_started",
                    status="succeeded",
                    proposal_json=m5_canonical_json(
                        {
                            "schema_version": "memory_proposal_result.v1",
                            "proposed_text_digest": preview_digest,
                            "evidence_refs": _source_refs(proof.readback),
                        }
                    ),
                    proposal_digest=m5_digest({"text_digest": preview_digest, "evidence": _source_refs(proof.readback)}),
                    expires_at=_now() + timedelta(minutes=15),
                )
                db.add(proposal_job)
        db.add(proposal)
        await db.flush()
        if status is MemoryProposalStatus.proposed:
            await _write_source_baseline(db, proof, proposal)
        return _proposal_payload(proposal)


async def list_memory_proposals(*, owner_principal_id: str, owner_session_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
    async with get_session() as db:
        statement = select(MemoryProposal).where(
            MemoryProposal.owner_principal_id == owner_principal_id,
            MemoryProposal.owner_session_id == owner_session_id,
        )
        if task_id:
            statement = statement.where(MemoryProposal.source_task_id == task_id)
        rows = (
            await db.execute(statement.order_by(MemoryProposal.created_at.desc(), MemoryProposal.proposal_id.desc()).limit(50))
        ).scalars().all()
        return [_proposal_payload(row) for row in rows]


async def list_work_board_decision_receipts(
    *, owner_principal_id: str, owner_session_id: str, task_id: str | None = None
) -> list[dict[str, Any]]:
    async with get_session() as db:
        statement = select(WorkBoardDecisionReceipt).where(
            WorkBoardDecisionReceipt.owner_principal_id == owner_principal_id,
            WorkBoardDecisionReceipt.owner_session_id == owner_session_id,
        )
        if task_id:
            statement = statement.where(WorkBoardDecisionReceipt.later_task_id == task_id)
        rows = (
            await db.execute(
                statement.order_by(
                    WorkBoardDecisionReceipt.created_at.desc(),
                    WorkBoardDecisionReceipt.receipt_id.desc(),
                ).limit(50)
            )
        ).scalars().all()
        return [_receipt_payload(row) for row in rows]


async def _canonical_accept(
    db: AsyncSession,
    proposal: MemoryProposal,
    *,
    actor_principal_id: str,
    actor_session_id: str,
    edited_text: str | None,
    decision_effect: MemoryProposalDecisionEffect,
    corrects_memory_id: str | None,
    preferred_capability_id: str | None,
) -> None:
    if proposal.status is not MemoryProposalStatus.proposed:
        raise ValueError("proposal_not_accepting")
    text = await sanitize_m5_memory_text_async(edited_text if edited_text is not None else proposal.preview_text or "")
    kind = proposal.memory_kind
    if kind not in {MemoryKind.fact, MemoryKind.pattern}:
        raise ValueError("memory_kind_invalid")
    scope = _decode_object(proposal.memory_scope_json)
    if not scope or scope.get("schema_version") != M5_SCOPE_SCHEMA_VERSION:
        raise ValueError("memory_scope_invalid")
    if preferred_capability_id:
        version = _capability_version(preferred_capability_id)
        if not version:
            raise ValueError("preferred_capability_unregistered")
        scope["preferred_capability_id"] = preferred_capability_id
        scope["preferred_capability_version"] = version
        scope["candidate_capability_ids"] = [preferred_capability_id]
        proposal.memory_scope_json = m5_canonical_json(scope)
    provenance = _decode_object(proposal.provenance_json)
    correction_target = None
    if corrects_memory_id:
        correction_target = (
            await db.execute(select(Memory).where(Memory.id == corrects_memory_id))
        ).scalar_one_or_none()
        if correction_target is None or correction_target.source_session_id != actor_session_id:
            raise PermissionError("correction_target_owner_mismatch")
        if correction_target.status is not MemoryStatus.active:
            raise ValueError("correction_target_unavailable")
        if (
            await db.execute(
                select(MemoryTombstone).where(MemoryTombstone.memory_id == correction_target.id)
            )
        ).scalar_one_or_none() is not None:
            raise ValueError("correction_target_deleted")
        if _canonical_memory_deletion_marker(correction_target) is not None:
            raise ValueError("correction_target_deleted")
        proposal.corrects_memory_id = correction_target.id
        provenance.update(
            {
                "corrects_memory_id": correction_target.id,
                "corrected_memory_previous_status": correction_target.status.value,
            }
        )
    # Memory.scope_key is unique by design.  Include the proposal identity so
    # a later, operator-reviewed correction can become a new canonical
    # version while the human-readable scope remains stable in provenance.
    scope_key = m5_digest({"scope": scope, "proposal_id": proposal.proposal_id})
    provenance.update(
        {
            "schema_version": M5_PROVENANCE_SCHEMA_VERSION,
            "proposal_id": proposal.proposal_id,
            "accepted_content_digest": m5_text_digest(text),
            "memory_kind": kind.value,
            "memory_scope": scope,
            "decision_effect": decision_effect.value,
        }
    )
    source_binding = _m5_verified_source_binding(proposal)
    if source_binding is None:
        raise ValueError("verified_source_binding_invalid")
    provenance["verified_source_binding"] = source_binding
    provenance["selection_binding_key_id"] = _m5_selection_binding_key_id()
    provenance["selection_binding_mac"] = _m5_selection_binding_mac(
        proposal_id=proposal.proposal_id,
        accepted_content_digest=m5_text_digest(text),
        owner_principal_id=str(provenance.get("owner_principal_id") or actor_principal_id),
        owner_session_id=str(provenance.get("owner_session_id") or actor_session_id),
        source_context_digest=str(
            provenance.get("source_context_digest") or scope.get("source_context_digest") or ""
        ),
        source_binding=source_binding,
        decision_effect=decision_effect,
        memory_scope=scope,
    )
    metadata_json = m5_canonical_json({"work_board_provenance": provenance})
    memory = await memory_repository.create_m5_memory_in_session(
        db,
        content=text,
        kind=kind,
        source_session_id=actor_session_id,
        scope_key=scope_key,
        metadata_json=metadata_json,
        confidence=float(proposal.confidence or 0.5),
        corrects_memory_id=proposal.corrects_memory_id,
        proposal_id=proposal.proposal_id,
    )
    proposal.accepted_memory_id = memory.id
    proposal.accepted_memory_content_digest = m5_text_digest(memory.content)
    proposal.preview_text = text
    proposal.preview_text_digest = m5_text_digest(text)
    proposal.decision_effect = decision_effect
    proposal.accepted_by_principal_id = actor_principal_id
    proposal.accepted_by_session_id = actor_session_id
    proposal.accepted_at = _now()
    proposal.status = MemoryProposalStatus.accepted
    proposal.recovery_action = "none"
    proposal.reason_code = "accepted"
    proposal.revision += 1
    proposal.updated_at = _now()
    db.add(proposal)


async def apply_memory_proposal_action(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    proposal_id: str,
    action: str,
    expected_revision: int,
    expected_preview_text_digest: str | None = None,
    expected_task_revision: int | None = None,
    expected_goal_revision: int | None = None,
    edited_text: str | None = None,
    decision_effect: MemoryProposalDecisionEffect | str | None = None,
    reason: str | None = None,
    corrects_memory_id: str | None = None,
    preferred_capability_id: str | None = None,
) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in {"accept", "edit_accept", "reject", "rollback", "recover"}:
        raise ValueError("unknown_proposal_action")
    normalized_rollback_reason: str | None = None
    if action == "rollback":
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
            raise ValueError("rollback_reason_invalid")
        normalized_rollback_reason = reason.strip()
    async with get_session() as db:
        from src.work_board.repository import _begin_sqlite_immediate

        await _begin_sqlite_immediate(db)
        proposal = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == proposal_id,
                    MemoryProposal.owner_principal_id == owner_principal_id,
                    MemoryProposal.owner_session_id == owner_session_id,
                )
            )
        ).scalar_one_or_none()
        if proposal is None:
            raise PermissionError("proposal_owner_mismatch")
        if action == "rollback" and proposal.status is MemoryProposalStatus.rolled_back:
            payload = _proposal_payload(proposal)
            payload["idempotent_replay"] = True
            return payload
        effect: MemoryProposalDecisionEffect | None = None
        acceptance_binding: str | None = None
        if action in {"accept", "edit_accept"}:
            try:
                effect = MemoryProposalDecisionEffect(decision_effect or MemoryProposalDecisionEffect.none)
            except ValueError as exc:
                raise ValueError("decision_effect_invalid") from exc
            if action == "accept" and edited_text is not None:
                raise ValueError("edited_text_requires_edit_accept")
            normalized_text = await sanitize_m5_memory_text_async(
                edited_text if edited_text is not None else proposal.preview_text or ""
            )
            target_id = (
                _safe_identifier(corrects_memory_id, field="corrects_memory_id")
                if corrects_memory_id
                else proposal.corrects_memory_id
            )
            selected_capability = (
                _safe_identifier(preferred_capability_id, field="preferred_capability_id")
                if preferred_capability_id
                else str(_decode_object(proposal.memory_scope_json).get("preferred_capability_id") or "")
            )
            if selected_capability and not _capability_version(selected_capability):
                raise ValueError("preferred_capability_unregistered")
            acceptance_binding = m5_digest(
                {
                    "version": M5_SCHEMA_VERSION,
                    "proposal_id": proposal.proposal_id,
                    "expected_revision": int(expected_revision),
                    "expected_preview_text_digest": str(expected_preview_text_digest or ""),
                    "action": action,
                    "accepted_text_digest": m5_text_digest(normalized_text),
                    "decision_effect": effect.value,
                    "corrects_memory_id": target_id or "",
                    "preferred_capability_id": selected_capability,
                }
            )
            if proposal.status is MemoryProposalStatus.accepted:
                if proposal.acceptance_binding_digest == acceptance_binding:
                    payload = _proposal_payload(proposal)
                    payload["idempotent_replay"] = True
                    return payload
                raise ValueError("proposal_already_accepted")
        if action == "reject" and proposal.status is MemoryProposalStatus.rejected:
            if (
                proposal.rejected_by_principal_id == owner_principal_id
                and int(proposal.revision or 0) == int(expected_revision) + 1
                and proposal.preview_text_digest == expected_preview_text_digest
                and proposal.reason_code == str(reason or "operator_rejected")[:200]
            ):
                payload = _proposal_payload(proposal)
                payload["idempotent_replay"] = True
                return payload
        if int(proposal.revision or 0) != int(expected_revision):
            raise ValueError("stale_proposal_revision")
        if action in {"accept", "edit_accept", "reject"}:
            if expected_task_revision is None or int(proposal.source_task_revision or 0) != int(expected_task_revision):
                raise ValueError("stale_task_revision")
            if expected_goal_revision is None or int(proposal.goal_revision or 0) != int(expected_goal_revision):
                raise ValueError("stale_goal_revision")
        if action in {"accept", "edit_accept", "reject"} and not expected_preview_text_digest:
            raise ValueError("preview_digest_required")
        if (
            action != "rollback"
            and expected_preview_text_digest is not None
            and proposal.preview_text_digest != expected_preview_text_digest
        ):
            raise ValueError("stale_preview_digest")
        if corrects_memory_id and action not in {"accept", "edit_accept"}:
            raise ValueError("correction_target_requires_accept")
        if action in {"accept", "edit_accept"}:
            if proposal.expires_at is not None and _utc(proposal.expires_at) <= _now():
                proposal.status = MemoryProposalStatus.expired
                proposal.reason_code = "proposal_expired"
                proposal.recovery_action = "request_verified_proposal_again"
                proposal.revision += 1
                proposal.updated_at = _now()
                db.add(proposal)
                await db.flush()
                audit_event = await _write_memory_action_audit(
                    db,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    proposal=proposal,
                    action="expire",
                )
                payload = _proposal_payload(proposal)
                payload["audit_event_id"] = audit_event.id
                return payload
            await _validate_current_proposal_source(
                db,
                proposal,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                expected_task_revision=int(expected_task_revision),
                expected_goal_revision=int(expected_goal_revision),
            )
            if corrects_memory_id is not None:
                proposal.corrects_memory_id = _safe_identifier(corrects_memory_id, field="corrects_memory_id")
            selected_capability = (
                _safe_identifier(preferred_capability_id, field="preferred_capability_id")
                if preferred_capability_id
                else str(_decode_object(proposal.memory_scope_json).get("preferred_capability_id") or "")
            ) or None
            try:
                await _canonical_accept(
                    db,
                    proposal,
                    actor_principal_id=owner_principal_id,
                    actor_session_id=owner_session_id,
                    edited_text=edited_text,
                    decision_effect=effect or MemoryProposalDecisionEffect.none,
                    corrects_memory_id=proposal.corrects_memory_id,
                    preferred_capability_id=selected_capability,
                )
            except CapabilityJournalError:
                # A server-key outage must never turn operator acceptance into
                # an HTTP 500 or an unsigned canonical memory.  Persist a
                # visible recovery state and return the normal typed proposal
                # payload so the API can surface re-review/re-accept guidance.
                proposal.status = MemoryProposalStatus.blocked
                proposal.reason_code = "accepted_binding_unavailable"
                proposal.recovery_action = "verify_source_and_reaccept"
                proposal.revision += 1
                proposal.updated_at = _now()
                db.add(proposal)
                await db.flush()
                payload = _proposal_payload(proposal)
                payload["error_code"] = "accepted_binding_unavailable"
                return payload
            proposal.acceptance_binding_digest = acceptance_binding
        elif action == "reject":
            if proposal.status is not MemoryProposalStatus.proposed:
                return _proposal_payload(proposal)
            proposal.status = MemoryProposalStatus.rejected
            proposal.reason_code = str(reason or "operator_rejected")[:200]
            proposal.rejected_by_principal_id = owner_principal_id
            proposal.rejected_by_session_id = owner_session_id
            proposal.rejected_at = _now()
            proposal.revision += 1
            proposal.updated_at = _now()
        elif action == "rollback":
            if proposal.status is not MemoryProposalStatus.accepted or not proposal.accepted_memory_id:
                raise ValueError("proposal_not_accepted")
            memory = (
                await db.execute(select(Memory).where(Memory.id == proposal.accepted_memory_id))
            ).scalar_one_or_none()
            if memory is None or memory.source_session_id != owner_session_id:
                raise PermissionError("accepted_memory_owner_mismatch")
            tombstone = (
                await db.execute(select(MemoryTombstone).where(MemoryTombstone.memory_id == memory.id))
            ).scalar_one_or_none()
            if tombstone is not None:
                raise ValueError("memory_tombstoned")
            if proposal.accepted_memory_content_digest != m5_text_digest(memory.content):
                raise ValueError("memory_changed_before_rollback")
            await memory_repository.rollback_m5_memory_in_session(
                db,
                memory_id=memory.id,
                expected_content_digest=proposal.accepted_memory_content_digest,
                expected_proposal_id=proposal.proposal_id,
            )
            proposal.status = MemoryProposalStatus.rolled_back
            proposal.reason_code = "rolled_back"
            proposal.rollback_by_principal_id = owner_principal_id
            proposal.rollback_by_session_id = owner_session_id
            proposal.rollback_at = _now()
            proposal.rollback_reason = normalized_rollback_reason or ""
            proposal.revision += 1
            proposal.updated_at = _now()
            affected_receipts = (
                await db.execute(
                    select(WorkBoardDecisionReceipt).where(
                        WorkBoardDecisionReceipt.accepted_memory_id == memory.id
                    )
                )
            ).scalars().all()
            receipt_updated_at = _now()
            for receipt in affected_receipts:
                receipt.decision_status = WorkBoardDecisionStatus.blocked
                receipt.admission_status = WorkBoardDecisionAdmissionStatus.superseded
                receipt.reason = "memory_rolled_back"
                receipt.revision += 1
                receipt.updated_at = receipt_updated_at
                receipt.receipt_integrity_mac = _m5_receipt_integrity_mac(receipt)
                db.add(receipt)
        else:
            raise ValueError("proposal_recovery_not_supported")
        audit_action = action
        audit_event = await _write_memory_action_audit(
            db,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            proposal=proposal,
            action=audit_action,
        )
        await db.flush()
        payload = _proposal_payload(proposal)
        payload["audit_event_id"] = audit_event.id
        return payload


async def redact_m5_memory_references(db: AsyncSession, memory_id: str) -> None:
    """Redact proposal candidate text and invalidate receipts after deletion."""

    proposals = (
        await db.execute(select(MemoryProposal).where(MemoryProposal.accepted_memory_id == memory_id))
    ).scalars().all()
    for proposal in proposals:
        proposal.privacy_state = MemoryProposalPrivacyState.redacted
        proposal.preview_text = None
        proposal.memory_scope_json = None
        proposal.provenance_json = m5_canonical_json({"schema_version": M5_PROVENANCE_SCHEMA_VERSION, "redacted": True})
        proposal.updated_at = _now()
        db.add(proposal)
    affected_receipts = (
        await db.execute(
            select(WorkBoardDecisionReceipt).where(
                WorkBoardDecisionReceipt.accepted_memory_id == memory_id
            )
        )
    ).scalars().all()
    receipt_updated_at = _now()
    for receipt in affected_receipts:
        receipt.decision_status = WorkBoardDecisionStatus.blocked
        receipt.admission_status = WorkBoardDecisionAdmissionStatus.superseded
        receipt.reason = "memory_deleted_or_export_redacted"
        receipt.updated_at = receipt_updated_at
        receipt.receipt_integrity_mac = _m5_receipt_integrity_mac(receipt)
        db.add(receipt)


__all__ = [
    "M5GoalDecision",
    "M5SourceProof",
    "apply_memory_proposal_action",
    "create_memory_proposal",
    "evaluate_goal_candidate_memory",
    "list_memory_proposals",
    "list_work_board_decision_receipts",
    "m5_candidate_action_ids",
    "m5_canonical_json",
    "m5_digest",
    "m5_goal_candidate_set_digest",
    "m5_goal_source_context_digest",
    "m5_memory_scope",
    "m5_source_context_digest",
    "m5_task_intent_digest",
    "m5_text_digest",
    "normalize_m5_memory_text",
    "redact_m5_memory_references",
    "sanitize_m5_memory_text",
    "sanitize_m5_memory_text_async",
    "validate_goal_candidate_requests",
]
