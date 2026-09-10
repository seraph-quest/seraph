"""Bounded durable invocation contract backed by ``WorkflowRunState``.

The workflow state table is the canonical execution record.  This module adds
the typed admission/lifecycle operations needed by capabilities without
introducing another queue or state machine.  It deliberately records hashes
and structural metadata for inputs, checkpoints, and results; callers must
store sensitive values in their existing governed stores.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import false, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import select

from src.artifacts.registry import build_artifact_record
from src.db.models import WorkflowRunState
from src.db.session_refs import ensure_sessions_exist


DURABLE_JOB_RECORD_SCHEMA_VERSION = 2

# Higher priority values are selected first by a future broker.  The contract
# itself only persists the value and never starts a second scheduler.
DURABLE_JOB_STATUSES = (
    "accepted",
    "queued",
    "running",
    "awaiting_approval",
    "paused",
    "blocked",
    # Restart recovery keeps uncertain external work in explicit, operator
    # reconciled states. Neither state is retryable until an outcome is known.
    "unknown_external_effect",
    "cost_liability",
    "failed",
    "succeeded",
    "cancelled",
)

DURABLE_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "accepted": frozenset({
        "queued",
        "blocked",
        "unknown_external_effect",
        "cost_liability",
        "failed",
        "cancelled",
    }),
    "queued": frozenset({
        "running",
        "blocked",
        "unknown_external_effect",
        "cost_liability",
        "failed",
        "cancelled",
    }),
    "running": frozenset({
        "awaiting_approval",
        "paused",
        "blocked",
        "unknown_external_effect",
        "cost_liability",
        "failed",
        "succeeded",
        "cancelled",
    }),
    # The transition is legal only through ``resume_approved_job`` (or an
    # equivalent caller that supplies the current approval binding).  The
    # generic resume operation remains fail-closed below.
    "awaiting_approval": frozenset({"queued", "blocked", "failed", "cancelled"}),
    "paused": frozenset({
        "queued",
        "blocked",
        "unknown_external_effect",
        "cost_liability",
        "failed",
        "cancelled",
    }),
    "blocked": frozenset({
        "queued",
        "unknown_external_effect",
        "cost_liability",
        "failed",
        "cancelled",
    }),
    "unknown_external_effect": frozenset({"blocked", "failed", "cancelled"}),
    "cost_liability": frozenset({"blocked", "failed", "cancelled"}),
    "failed": frozenset({"queued"}),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
}
DURABLE_JOB_TERMINAL_STATUSES = frozenset({"succeeded", "cancelled"})
UNCERTAIN_EXTERNAL_EFFECT_STATUSES = frozenset({"unknown_external_effect", "cost_liability"})
UNRESOLVED_EFFECT_STATUSES = frozenset({"unknown", "intent", "dispatched"})
DEPENDENCY_FAILURE_STATUSES = frozenset({"failed", "cancelled"})
DEPENDENCY_UNRESOLVED_STATUSES = frozenset({
    "unknown_external_effect",
    "cost_liability",
})
UNSAFE_RETRY_REASONS = frozenset({
    "unknown_external_effect",
    "cost_liability",
    "stale_lease_unknown_external_effect",
    "stale_lease_cost_liability",
})
# These failures may have crossed a remote/effect boundary. An empty or
# corrupt ledger cannot be treated as proof that no effect occurred.
EFFECT_RECONCILIATION_REQUIRED_REASONS = frozenset({
    "dispatch_timeout",
    "provider_timeout",
    "provider_error",
    "remote_timeout",
    "remote_error",
    "unknown_external_effect",
    "cost_liability",
    "stale_lease_unknown_external_effect",
    "stale_lease_cost_liability",
})

# Remote admission remains process-local, but an existing durable job may keep
# an operator-safe projection of its admission lifecycle in the canonical
# effect ledger.  These values describe the broker receipt; they are not a
# second durable job state machine.
REMOTE_INFERENCE_RECEIPT_STATUSES = frozenset(
    {"queued", "running", "blocked", "succeeded", "settled", "failed", "cancelled", "expired", "rejected"}
)
REMOTE_INFERENCE_EFFECT_STATUSES = {
    "queued": "unknown",
    "running": "unknown",
    "blocked": "blocked",
    "succeeded": "succeeded",
    "settled": "succeeded",
    "failed": "failed",
    "cancelled": "failed",
    "expired": "failed",
    "rejected": "failed",
}
RECONCILIATION_RECEIPT_STATUSES = frozenset({"read_back", "settled", "reconciled"})
RECONCILIATION_RECEIPT_FIELDS = (
    "schema_version",
    "effect_id",
    "effect_type",
    "target_path",
    "status",
    "outcome",
    "actual_cost_microusd",
    "readback_digest",
    "target_digest",
    "provider_operation_id",
    "observed_at",
    "operator_id",
    "adapter_idempotency_key",
)
REMOTE_INFERENCE_RECEIPT_FIELDS = (
    "schema_version",
    "operation_id",
    "job_id",
    "owner_id",
    "parent_job_id",
    "runtime_path",
    "priority",
    "priority_rank",
    "status",
    "queue_position",
    "active_operation_id",
    "fencing_token",
    "reason_code",
    "queued",
    "max_queued",
    "serial_gpu",
    "resource_class",
    "serial_remote_inference",
    "cancel_requested",
    "reconciliation_required",
    "recovery_action",
    "deadline_exceeded",
    "callback_completed",
    "capability_version",
    "owner_outstanding",
    "max_owner_outstanding",
    "cost_reserved_microusd",
    "owner_cost_budget_microusd",
    "unknown_cost_outstanding",
    "cost_settled_microusd",
    "operator_visible",
)


class DurableJobError(RuntimeError):
    """Base error for rejected durable job operations."""


class DurableJobNotFound(DurableJobError):
    pass


class DurableJobTransitionError(DurableJobError, ValueError):
    pass


class DurableJobIdempotencyConflict(DurableJobError, ValueError):
    pass


class DurableJobLeaseError(DurableJobError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("deadline_at must be an ISO timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _string_list(value: Iterable[Any] | None) -> list[str]:
    if value is None:
        return []
    return sorted({str(item).strip() for item in value if str(item or "").strip()})


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _effect_ledger_or_raise(raw: str | None) -> list[dict[str, Any]]:
    """Load effect history without treating corruption as an empty ledger."""
    if raw is None or not str(raw).strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DurableJobTransitionError(
            "durable effect history is malformed; reconciliation is required"
        ) from exc
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise DurableJobTransitionError(
            "durable effect history is malformed; reconciliation is required"
        )
    return parsed


def _rowcount_is_one(result: Any) -> bool:
    """Treat exactly one affected durable row as a successful CAS write."""
    return getattr(result, "rowcount", None) == 1


def _revision(run: WorkflowRunState) -> int:
    """Read a migrated row revision while tolerating pre-CAS test doubles."""
    return max(int(getattr(run, "revision", 0) or 0), 0)


def _job_has_unsafe_effects(effects: Any) -> bool:
    """Return true when an effect ledger still needs external reconciliation."""
    for item in effects if isinstance(effects, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("reconciled") is True or item.get("reconciliation_status") in {
            "reconciled",
            "resolved",
        }:
            continue
        status = _text(item.get("status"))
        if status in UNRESOLVED_EFFECT_STATUSES:
            return True
        details = item.get("details")
        if isinstance(details, dict):
            if details.get("reconciliation_required") or details.get("unknown_cost_outstanding"):
                return True
            nested = details.get("receipt")
            if isinstance(nested, dict) and (
                nested.get("reconciliation_required") or nested.get("unknown_cost_outstanding")
            ):
                return True
    return False


def _effect_is_unresolved(item: Any) -> bool:
    """Return whether one receipt still carries an external liability."""
    if not isinstance(item, dict):
        return False
    if item.get("reconciled") is True or item.get("reconciliation_status") in {
        "reconciled",
        "resolved",
    }:
        return False
    if _text(item.get("status")) in UNRESOLVED_EFFECT_STATUSES:
        return True
    details = item.get("details")
    if isinstance(details, dict):
        if details.get("reconciliation_required") or details.get("unknown_cost_outstanding"):
            return True
        nested = details.get("receipt")
        if isinstance(nested, dict) and (
            nested.get("reconciliation_required") or nested.get("unknown_cost_outstanding")
        ):
            return True
    return False


def _bounded_effect_ledger(effects: list[dict[str, Any]], *, limit: int = 100) -> list[dict[str, Any]]:
    """Bound settled history while retaining every unresolved receipt."""
    unresolved = [item for item in effects if _effect_is_unresolved(item)]
    settled = [item for item in effects if not _effect_is_unresolved(item)]
    retained_settled = max(limit - len(unresolved), 0)
    return unresolved + (settled[-retained_settled:] if retained_settled else [])


def _verified_readback_exists(effects: Any) -> bool:
    """Require a capability-specific, positive readback before success."""
    for item in effects if isinstance(effects, list) else []:
        if not isinstance(item, dict) or _text(item.get("receipt_kind")) != "readback":
            continue
        if _text(item.get("status")) != "succeeded":
            continue
        if not _text(item.get("target_path")):
            continue
        details = item.get("details")
        details_verified = isinstance(details, dict) and details.get("verified") is True
        goal_verified = isinstance(details, dict) and all(
            details.get(field_name) is True
            for field_name in ("output_exists", "workspace_contained", "goal_id_read_back")
        )
        if (details_verified or goal_verified) and (
            _text(item.get("content_sha256"))
            or _text(item.get("target_digest"))
            or _text(item.get("readback_digest"))
        ):
            return True
    return False


def _effect_recovery_state(effects: list[dict[str, Any]]) -> tuple[str, str]:
    """Classify an unresolved ledger for operator-visible recovery."""
    for item in effects:
        if not _effect_is_unresolved(item):
            continue
        details = item.get("details") if isinstance(item, dict) else None
        nested = details.get("receipt") if isinstance(details, dict) else None
        if (
            isinstance(details, dict)
            and details.get("unknown_cost_outstanding")
        ) or (
            isinstance(nested, dict) and nested.get("unknown_cost_outstanding")
        ):
            return "cost_liability", "cost_liability"
    return "unknown_external_effect", "unknown_external_effect"


def _restart_recovery_state(run: WorkflowRunState) -> tuple[str, str]:
    """Classify a stale run without assuming an external callback was harmless."""
    try:
        effects = _effect_ledger_or_raise(getattr(run, "effect_receipts_json", None))
    except DurableJobTransitionError:
        return "blocked", "malformed_effect_history_requires_reconciliation"
    if _job_has_unsafe_effects(effects):
        status, reason = _effect_recovery_state(effects)
        return status, f"stale_lease_{reason}"
    return "blocked", "stale_lease_requires_reconciliation"


def _safe_structure(value: Any, *, max_depth: int = 3) -> Any:
    """Keep durable receipts structural and never persist secret values."""
    if max_depth <= 0:
        return "[redacted]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            if any(
                marker in normalized_key
                for marker in (
                    "secret",
                    "token",
                    "password",
                    "credential",
                    "apikey",
                    "privatekey",
                    "authorization",
                    "authheader",
                )
            ) or normalized_key in {"originalerror", "errordetail", "traceback"}:
                result[key_text] = "[redacted]"
            else:
                result[key_text] = _safe_structure(item, max_depth=max_depth - 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_structure(item, max_depth=max_depth - 1) for item in list(value)[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_inputs_digest(inputs: Any) -> tuple[str, dict[str, Any]]:
    keys = sorted(str(key) for key in inputs.keys()) if isinstance(inputs, dict) else []
    return _digest(inputs), {"redacted": True, "keys": keys, "shape": type(inputs).__name__}


def _binding(
    *,
    owner_principal_id: str,
    goal_id: str | None,
    goal_revision: int | None,
    idempotency_scope: str,
    dedupe_key: str,
) -> str:
    return _digest({
        "owner_principal_id": owner_principal_id,
        "goal_id": goal_id or "",
        "goal_revision": goal_revision,
        "idempotency_scope": idempotency_scope,
        "candidate_dedupe_key": dedupe_key,
    })


def _validate_owner_fields(
    *, owner_kind: str, owner_principal_id: str, service_id: str | None
) -> None:
    """Validate the small owner identity shape this repository persists."""
    if owner_kind not in {"user", "service"}:
        raise ValueError("owner_kind must be user or service")
    if not _text(owner_principal_id):
        raise ValueError("owner_principal_id is required")
    if owner_kind == "service" and not _text(service_id):
        raise ValueError("service jobs require service_id")
    if owner_kind == "user" and _text(service_id):
        raise ValueError("user jobs cannot set service_id")


def _validate_admission_authority(spec: "DurableJobSpec") -> None:
    identity = spec.identity
    _validate_owner_fields(
        owner_kind=identity.owner_kind,
        owner_principal_id=identity.owner_principal_id,
        service_id=spec.service_id,
    )
    authority = spec.declared_authority
    authority_principal = authority.get("principal")
    if _text(authority_principal) != identity.owner_principal_id:
        raise ValueError("declared authority principal must match owner_principal_id")
    authority_kind = authority.get("owner_kind", authority.get("kind"))
    if authority_kind is not None and _text(authority_kind) != identity.owner_kind:
        raise ValueError("declared authority owner kind must match owner_kind")
    authority_service_id = authority.get("service_id")
    if authority_service_id is not None and _text(authority_service_id) != _text(spec.service_id):
        raise ValueError("declared authority service_id must match service_id")
    if identity.owner_kind == "service" and _text(authority_service_id) != _text(spec.service_id):
        raise ValueError("service authority must declare the matching service_id")
    if identity.owner_kind == "user" and _text(authority_service_id):
        raise ValueError("user authority cannot declare service_id")


def _deadline_identity(value: datetime | str | None) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed else None


def _deadline_expired(run: WorkflowRunState, *, now: datetime | None = None) -> bool:
    """Return whether a persisted job deadline has passed."""
    try:
        deadline = _as_utc(getattr(run, "deadline_at", None))
    except ValueError as exc:
        raise DurableJobTransitionError("job deadline metadata is malformed") from exc
    return deadline is not None and deadline <= (now or _utc_now())


def _bounded_identifier(value: Any, *, field_name: str, limit: int = 512) -> str:
    """Normalize receipt identity without retaining unbounded/control text."""
    normalized = _text(value)
    if not normalized:
        return ""
    if len(normalized) > limit or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return normalized


def _append_parent_fence_condition(
    conditions: list[Any], run: WorkflowRunState, *, now: datetime
) -> None:
    """Require a child to execute under the exact live parent fence."""
    parent_job_id = _text(getattr(run, "parent_job_id", None))
    if not parent_job_id:
        return
    try:
        parent_fence = int(getattr(run, "parent_fencing_token"))
    except (TypeError, ValueError):
        conditions.append(false())
        return
    if parent_fence <= 0:
        conditions.append(false())
        return
    parent = aliased(WorkflowRunState)
    conditions.append(
        select(parent.id)
        .where(
            parent.run_identity == parent_job_id,
            parent.status == "running",
            parent.lease_owner.is_not(None),
            parent.lease_expires_at > now,
            parent.fencing_token == parent_fence,
        )
        .exists()
    )


def _normalized_json_list(raw: str | None) -> str:
    parsed = _json_load(raw, None)
    if not isinstance(parsed, list):
        return "<invalid>"
    return _canonical(_string_list(parsed))


def _dependency_ids(run: WorkflowRunState) -> list[str]:
    """Read the persisted dependency set without hiding malformed metadata."""
    raw = getattr(run, "dependencies_json", None)
    if raw is None or not str(raw).strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DurableJobTransitionError(
            "durable dependency metadata is malformed; recovery is required"
        ) from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise DurableJobTransitionError(
            "durable dependency metadata is malformed; recovery is required"
        )
    return _string_list(parsed)


def _admission_conflicts(
    existing: WorkflowRunState,
    *,
    spec: "DurableJobSpec",
    input_digest: str,
    authority_digest: str,
    deadline: datetime | None,
) -> list[str]:
    """Return immutable admission fields that differ without exposing values."""
    identity = spec.identity
    expected = {
        "job_id": identity.job_id,
        "input_digest": input_digest,
        "job_kind": identity.job_kind,
        "capability_version": identity.capability_version,
        "owner_kind": identity.owner_kind,
        "owner_principal_id": identity.owner_principal_id,
        "service_id": spec.service_id,
        "authority_digest": authority_digest,
        "dependencies": _canonical(_string_list(spec.dependencies)),
        "resource_claims": _canonical(_string_list(spec.resource_claims)),
        "deadline_at": _deadline_identity(deadline),
        "priority": int(spec.priority),
        "max_attempts": int(spec.max_attempts),
        "session_id": spec.session_id,
        "parent_job_id": spec.parent_job_id,
        "parent_fencing_token": spec.parent_fencing_token,
        "goal_id": spec.goal_id,
        "goal_revision": spec.goal_revision,
        "plan_revision": spec.plan_revision,
        "candidate_id": spec.candidate_id,
    }
    actual = {
        "job_id": getattr(existing, "run_identity", None),
        "input_digest": getattr(existing, "input_digest", None),
        "job_kind": getattr(existing, "job_kind", None),
        "capability_version": getattr(existing, "capability_version", None),
        "owner_kind": getattr(existing, "owner_kind", None),
        "owner_principal_id": getattr(existing, "owner_principal_id", None),
        "service_id": getattr(existing, "service_id", None),
        "authority_digest": getattr(existing, "authority_digest", None),
        "dependencies": _normalized_json_list(getattr(existing, "dependencies_json", None)),
        "resource_claims": _normalized_json_list(getattr(existing, "resource_claims_json", None)),
        "deadline_at": _deadline_identity(getattr(existing, "deadline_at", None)),
        "priority": int(getattr(existing, "priority", 0) or 0),
        "max_attempts": int(getattr(existing, "max_attempts", 0) or 0),
        "session_id": getattr(existing, "session_id", None),
        "parent_job_id": getattr(existing, "parent_job_id", None),
        "parent_fencing_token": getattr(existing, "parent_fencing_token", None),
        "goal_id": getattr(existing, "goal_id", None),
        "goal_revision": getattr(existing, "goal_revision", None),
        "plan_revision": getattr(existing, "plan_revision", None),
        "candidate_id": getattr(existing, "candidate_id", None),
    }
    # Scheduled child work is deliberately deduped across strategist parent
    # occurrences.  The first admitted child retains its original lineage;
    # a later parent may replay that same child only after its own fence has
    # been checked.  Other callers keep parent identity immutable.
    if identity.idempotency_scope == "goal-snapshot-to-file-scheduler":
        for field_name in ("parent_job_id", "parent_fencing_token", "deadline_at"):
            expected.pop(field_name, None)
            actual.pop(field_name, None)
    return [field_name for field_name, value in expected.items() if actual[field_name] != value]


def _canonical_reconciliation_receipt(value: Any) -> tuple[str, str]:
    """Return a typed, redacted receipt for one externally observed effect.

    A generic acknowledgement is not enough to clear a restart liability. The
    receipt must identify the exact effect and use a definitive readback or
    settlement status.  Unknown provider tokens are represented by digests or
    the allowlisted structural fields below, never copied into durable state.
    """
    if not isinstance(value, Mapping) or not value:
        raise ValueError("reconciliation_receipt must be a nonempty mapping")
    raw = dict(value)
    effect_id = _bounded_identifier(raw.get("effect_id"), field_name="effect_id")
    if not effect_id:
        raise ValueError("reconciliation_receipt requires effect_id")
    status = _text(raw.get("status"))
    if status not in RECONCILIATION_RECEIPT_STATUSES:
        raise ValueError(
            "reconciliation_receipt status must be read_back, settled, or reconciled"
        )
    effect_type = _bounded_identifier(raw.get("effect_type"), field_name="effect_type")
    if not effect_type:
        raise ValueError("reconciliation_receipt requires effect_type")
    target_path = _bounded_identifier(raw.get("target_path"), field_name="target_path")
    outcome = _bounded_identifier(raw.get("outcome"), field_name="outcome")
    readback_digest = _bounded_identifier(raw.get("readback_digest"), field_name="readback_digest")
    operator_id = _bounded_identifier(raw.get("operator_id"), field_name="operator_id")
    if status in {"read_back", "reconciled"} and not outcome and not readback_digest:
        raise ValueError("read_back reconciliation requires outcome or readback_digest")
    actual_cost = raw.get("actual_cost_microusd")
    if actual_cost is not None:
        if isinstance(actual_cost, bool) or (
            isinstance(actual_cost, float)
            and (not math.isfinite(actual_cost) or not actual_cost.is_integer())
        ):
            raise ValueError("actual_cost_microusd must be a nonnegative integer")
        try:
            actual_cost = int(actual_cost)
        except (TypeError, ValueError) as exc:
            raise ValueError("actual_cost_microusd must be a nonnegative integer") from exc
        if actual_cost < 0:
            raise ValueError("actual_cost_microusd must be a nonnegative integer")
    provider_operation_id = _bounded_identifier(
        raw.get("provider_operation_id"), field_name="provider_operation_id"
    )
    adapter_idempotency_key = _bounded_identifier(
        raw.get("adapter_idempotency_key"), field_name="adapter_idempotency_key"
    )
    if status == "settled":
        if actual_cost is None:
            raise ValueError("settled reconciliation requires actual_cost_microusd")
        if not provider_operation_id and not adapter_idempotency_key:
            raise ValueError(
                "settled reconciliation requires provider_operation_id or adapter_idempotency_key"
            )
    normalized = {
        field_name: raw[field_name]
        for field_name in RECONCILIATION_RECEIPT_FIELDS
        if field_name in raw
    }
    normalized.update(
        {
            "effect_id": effect_id,
            "effect_type": effect_type,
            "target_path": target_path,
            "outcome": outcome,
            "readback_digest": readback_digest,
            "operator_id": operator_id,
        }
    )
    safe = _safe_structure(normalized)
    safe.update({"effect_id": effect_id, "effect_type": effect_type, "status": status})
    if actual_cost is not None:
        safe["actual_cost_microusd"] = actual_cost
    if provider_operation_id:
        safe["provider_operation_id"] = provider_operation_id
    if adapter_idempotency_key:
        safe["adapter_idempotency_key"] = adapter_idempotency_key
    canonical = _canonical(safe)
    return canonical, _digest(safe)


def _reconciliation_matches_effect(
    effect: Mapping[str, Any], receipt: Mapping[str, Any]
) -> None:
    """Require a reconciliation receipt to describe the exact ledger effect.

    A receipt that merely has a valid shape must not be allowed to settle a
    different operation.  This helper is shared by recovery and retry so a
    settled/verified effect cannot later be paired with an unrelated receipt.
    """
    if _text(effect.get("effect_id")) != _text(receipt.get("effect_id")) or _text(
        effect.get("effect_type")
    ) != _text(receipt.get("effect_type")):
        raise DurableJobTransitionError(
            "reconciliation_receipt must identify one durable effect"
        )
    receipt_status = _text(receipt.get("status"))
    if receipt_status in {"read_back", "reconciled"}:
        observed_path = _text(receipt.get("target_path"))
        expected_path = _text(effect.get("target_path"))
        if not observed_path or not expected_path or observed_path != expected_path:
            raise DurableJobTransitionError(
                "readback reconciliation requires the exact effect target"
            )
        observed_digest = _text(receipt.get("target_digest"))
        expected_digest = _text(effect.get("target_digest"))
        if observed_digest and expected_digest and observed_digest != expected_digest:
            raise DurableJobIdempotencyConflict(
                "readback reconciliation target digest does not match the intended effect"
            )
    if receipt_status == "settled":
        details = effect.get("details") if isinstance(effect.get("details"), dict) else {}
        nested = details.get("receipt") if isinstance(details, dict) else None
        expected_operation = _text(effect.get("provider_operation_id")) or _text(
            nested.get("operation_id") if isinstance(nested, dict) else None
        )
        expected_adapter_key = _text(effect.get("adapter_idempotency_key")) or _text(
            nested.get("adapter_idempotency_key") if isinstance(nested, dict) else None
        )
        observed_operation = _text(receipt.get("provider_operation_id"))
        observed_adapter_key = _text(receipt.get("adapter_idempotency_key"))
        if not (
            (observed_operation and observed_operation == expected_operation)
            or (observed_adapter_key and observed_adapter_key == expected_adapter_key)
        ):
            raise DurableJobTransitionError(
                "settled reconciliation must bind the exact provider operation or adapter key"
            )


def _validate_no_effect_retry_receipt(job_id: str, receipt: Mapping[str, Any]) -> None:
    """Bind a no-dispatch retry proof to this exact failed job.

    Failed jobs with no effect ledger can be retried only after an explicit
    typed observation that this invocation never crossed an external boundary.
    A destination readback for another operation is not such a proof.
    """
    expected_effect_id = f"job-failure:{job_id}"
    expected_target = f"job:{job_id}"
    if (
        _text(receipt.get("effect_id")) != expected_effect_id
        or _text(receipt.get("effect_type")) != "job_failure"
        or _text(receipt.get("target_path")) != expected_target
        or _text(receipt.get("status")) not in {"read_back", "reconciled"}
        or _text(receipt.get("outcome")) not in {"no_external_effect", "not_dispatched"}
    ):
        raise DurableJobTransitionError(
            "retry without an effect requires a job-bound no_external_effect reconciliation receipt"
        )


def _canonical_remote_inference_receipt(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Allowlist and redact a typed remote admission receipt before storage."""
    if not isinstance(value, Mapping):
        raise ValueError("remote inference receipt must be a mapping")
    raw = dict(value)
    status = _text(raw.get("status"))
    if status not in REMOTE_INFERENCE_RECEIPT_STATUSES:
        raise ValueError(f"unsupported remote inference receipt status: {status or '<empty>'}")
    required = ("operation_id", "job_id", "owner_id")
    missing = [field_name for field_name in required if not _text(raw.get(field_name))]
    if missing:
        raise ValueError("remote inference receipt requires " + ", ".join(missing))
    safe = _safe_structure(
        {field_name: raw[field_name] for field_name in REMOTE_INFERENCE_RECEIPT_FIELDS if field_name in raw}
    )
    # The status is validated above and normalized explicitly so a custom
    # mapping cannot smuggle a non-string status into the durable projection.
    safe["status"] = status
    return safe, _digest(safe)


def _validate_retry_actor(
    run: WorkflowRunState,
    *,
    owner_kind: str,
    owner_principal_id: str,
    service_id: str | None,
) -> None:
    """Require the retry actor to be the persisted job owner identity."""
    _validate_owner_fields(
        owner_kind=owner_kind,
        owner_principal_id=owner_principal_id,
        service_id=service_id,
    )
    if (
        getattr(run, "owner_kind", None) != owner_kind
        or getattr(run, "owner_principal_id", None) != owner_principal_id
        or getattr(run, "service_id", None) != service_id
    ):
        raise DurableJobLeaseError("retry actor is not the authenticated job owner")


def _authority_approval_id(authority: Any) -> str:
    """Read the approval identifier from the structured authority envelope."""
    if not isinstance(authority, Mapping):
        return ""
    direct = _text(authority.get("approval_id"))
    if direct:
        return direct
    nested = authority.get("approval")
    if isinstance(nested, Mapping):
        return _text(nested.get("approval_id") or nested.get("id"))
    return ""


def _authority_budget_microusd(authority: Any) -> int | None:
    """Return the persisted owner budget, preserving an absent budget as None."""
    if not isinstance(authority, Mapping):
        return None
    value: Any = None
    found = False
    for field_name in (
        "budget_microusd",
        "max_budget_microusd",
        "owner_cost_budget_microusd",
    ):
        if field_name in authority:
            value = authority[field_name]
            found = True
            break
    if not found and isinstance(authority.get("budget"), Mapping):
        budget = authority["budget"]
        for field_name in ("microusd", "max_microusd", "amount_microusd"):
            if field_name in budget:
                value = budget[field_name]
                found = True
                break
    if not found or value is None:
        return None
    if isinstance(value, bool):
        raise DurableJobTransitionError("durable approval budget metadata is malformed")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DurableJobTransitionError("durable approval budget metadata is malformed") from exc
    if parsed < 0:
        raise DurableJobTransitionError("durable approval budget metadata is malformed")
    return parsed


def _validate_approval_resume_receipt(
    run: WorkflowRunState,
    receipt: Any,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Validate a current authenticated approval for an approval-held job.

    Approval state is intentionally checked against the immutable durable
    authority/goal/plan/capability/budget projection.  This leaves a narrow
    adapter seam for the existing approval API without allowing a stale
    ``approved`` flag to make an old run runnable.
    """
    if not isinstance(receipt, Mapping):
        raise DurableJobTransitionError(
            "illegal approval resume requires a current authenticated approval"
        )
    raw = dict(receipt)
    if _text(raw.get("status")) != "approved":
        raise DurableJobTransitionError("approval resume requires an approved receipt")
    if raw.get("authenticated") is not True or raw.get("revoked") is True:
        raise DurableJobTransitionError("approval resume requires a current authenticated operator")
    operator_principal_id = _bounded_identifier(
        raw.get("operator_principal_id"), field_name="operator_principal_id"
    )
    operator_session_id = _bounded_identifier(
        raw.get("operator_session_id"), field_name="operator_session_id"
    )
    if not operator_principal_id or not operator_session_id:
        raise DurableJobTransitionError(
            "approval resume requires authenticated operator and session identities"
        )
    owner_kind = _text(raw.get("owner_kind"))
    owner_principal_id = _bounded_identifier(
        raw.get("owner_principal_id"), field_name="owner_principal_id"
    )
    service_id = _bounded_identifier(raw.get("service_id"), field_name="service_id") or None
    try:
        _validate_owner_fields(
            owner_kind=owner_kind,
            owner_principal_id=owner_principal_id,
            service_id=service_id,
        )
    except ValueError as exc:
        raise DurableJobTransitionError("approval resume owner binding is malformed") from exc
    if (
        owner_kind != _text(getattr(run, "owner_kind", None))
        or owner_principal_id != _text(getattr(run, "owner_principal_id", None))
        or service_id != (_text(getattr(run, "service_id", None)) or None)
    ):
        raise DurableJobTransitionError("approval resume owner binding is stale")
    approval_id = _bounded_identifier(raw.get("approval_id"), field_name="approval_id")
    authority = _json_load(getattr(run, "declared_authority_json", None), {})
    expected_approval_id = _authority_approval_id(authority)
    if not approval_id or not expected_approval_id or approval_id != expected_approval_id:
        raise DurableJobTransitionError("approval resume binding does not match the durable authority")
    if _text(raw.get("authority_digest")) != _text(getattr(run, "authority_digest", None)):
        raise DurableJobTransitionError("approval resume authority has changed")
    required_fields = ("goal_id", "goal_revision", "plan_revision", "capability_version", "budget_microusd")
    missing = [field_name for field_name in required_fields if field_name not in raw]
    if missing:
        raise DurableJobTransitionError(
            "approval resume is missing current " + ", ".join(missing)
        )
    for field_name in ("goal_id", "goal_revision", "plan_revision", "capability_version"):
        expected = getattr(run, field_name, None)
        actual = raw.get(field_name)
        if field_name in {"goal_revision", "plan_revision"}:
            try:
                actual = int(actual) if actual is not None else None
            except (TypeError, ValueError) as exc:
                raise DurableJobTransitionError(
                    f"approval resume {field_name} is malformed"
                ) from exc
        if actual != expected:
            raise DurableJobTransitionError(f"approval resume {field_name} is stale")
    expected_budget = _authority_budget_microusd(authority)
    expected_budget_digest = _digest({"budget_microusd": expected_budget})
    if _text(raw.get("budget_digest")) != expected_budget_digest:
        raise DurableJobTransitionError("approval resume budget digest is stale")
    actual_budget = raw.get("budget_microusd")
    if actual_budget is not None:
        if isinstance(actual_budget, bool):
            raise DurableJobTransitionError("approval resume budget is malformed")
        try:
            actual_budget = int(actual_budget)
        except (TypeError, ValueError) as exc:
            raise DurableJobTransitionError("approval resume budget is malformed") from exc
        if actual_budget < 0:
            raise DurableJobTransitionError("approval resume budget is malformed")
    if actual_budget != expected_budget:
        raise DurableJobTransitionError("approval resume budget is stale")
    try:
        expires_at = float(raw.get("expires_at"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DurableJobTransitionError("approval resume expiry is malformed") from exc
    if not math.isfinite(expires_at) or expires_at <= now.timestamp():
        raise DurableJobTransitionError("approval resume approval has expired")
    return {
        "kind": "approval_resume",
        "status": "approved",
        "approval_id": approval_id,
        "operator_principal_id": operator_principal_id,
        "operator_session_id": operator_session_id,
        "owner_kind": owner_kind,
        "owner_principal_id": owner_principal_id,
        "service_id": service_id,
        "authority_digest": _text(getattr(run, "authority_digest", None)),
        "goal_id": getattr(run, "goal_id", None),
        "goal_revision": getattr(run, "goal_revision", None),
        "plan_revision": getattr(run, "plan_revision", None),
        "capability_version": getattr(run, "capability_version", None),
        "budget_microusd": expected_budget,
        "budget_digest": expected_budget_digest,
        "expires_at": expires_at,
        "recorded_at": now.isoformat(),
    }


def _serialize(run: WorkflowRunState, *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an operator-safe durable job projection."""
    payload = {
        "job_id": run.run_identity,
        "run_identity": run.run_identity,
        "record_schema_version": int(getattr(run, "record_schema_version", DURABLE_JOB_RECORD_SCHEMA_VERSION) or 0),
        "parent_job_id": getattr(run, "parent_job_id", None),
        "parent_fencing_token": getattr(run, "parent_fencing_token", None),
        "owner": {
            "kind": getattr(run, "owner_kind", "legacy"),
            "principal_id": getattr(run, "owner_principal_id", None),
            "service_id": getattr(run, "service_id", None),
        },
        "job_kind": getattr(run, "job_kind", "workflow"),
        "capability_version": getattr(run, "capability_version", "workflow-v1"),
        "workflow_name": run.workflow_name,
        "tool_name": run.tool_name,
        "session_id": run.session_id,
        "goal_id": getattr(run, "goal_id", None),
        "goal_revision": getattr(run, "goal_revision", None),
        "plan_revision": getattr(run, "plan_revision", None),
        "candidate_id": getattr(run, "candidate_id", None),
        "status": run.status,
        "priority": int(getattr(run, "priority", 50) or 0),
        "dependencies": _json_load(getattr(run, "dependencies_json", None), []),
        "resource_claims": _json_load(getattr(run, "resource_claims_json", None), []),
        "input_digest": getattr(run, "input_digest", None),
        "authority_digest": getattr(run, "authority_digest", None),
        "declared_authority": _json_load(getattr(run, "declared_authority_json", None), {}),
        "idempotency": {
            "scope": getattr(run, "idempotency_scope", None),
            "key": getattr(run, "idempotency_key", None),
            "binding": getattr(run, "idempotency_binding", None),
        },
        "deadline_at": run.deadline_at.isoformat() if getattr(run, "deadline_at", None) else None,
        "lease": {
            "owner": getattr(run, "lease_owner", None),
            "expires_at": run.lease_expires_at.isoformat() if getattr(run, "lease_expires_at", None) else None,
            "fencing_token": int(getattr(run, "fencing_token", 0) or 0),
        },
        "revision": _revision(run),
        "attempt_count": int(getattr(run, "attempt_count", 0) or 0),
        "max_attempts": int(getattr(run, "max_attempts", 1) or 1),
        "failure_reason": getattr(run, "failure_reason", None),
        "result": {
            "digest": getattr(run, "result_digest", None),
            "summary": getattr(run, "result_summary", None),
        },
        "checkpoints": _json_load(getattr(run, "checkpoint_receipts_json", None), []),
        "artifacts": _json_load(getattr(run, "artifact_receipts_json", None), []),
        "effects": _json_load(getattr(run, "effect_receipts_json", None), []),
        "started_at": run.started_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "claim_boundary": "durable_job_contract_on_workflow_state_not_exactly_once_external_execution",
    }
    if receipt is not None:
        payload["receipt"] = receipt
    return payload


def _deduped_admission(existing: WorkflowRunState, *, binding: str) -> dict[str, Any]:
    """Return the canonical row for a repeated admission attempt.

    The unique index is the final race authority.  Both the read-before-insert
    path and the insert-conflict path must return the same operator receipt.
    """
    receipt = {
        "kind": "job_admission",
        "status": "deduped",
        "job_id": existing.run_identity,
        "idempotency_binding": binding,
        "terminal_noop": existing.status in DURABLE_JOB_TERMINAL_STATUSES,
        "operator_visible": True,
    }
    return _serialize(existing, receipt=receipt)


@dataclass(frozen=True, slots=True)
class DurableJobIdentity:
    """Stable identity fields used for idempotent admission."""

    job_id: str
    owner_kind: str
    owner_principal_id: str
    job_kind: str
    capability_version: str
    idempotency_scope: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in (
            "job_id",
            "owner_kind",
            "owner_principal_id",
            "job_kind",
            "capability_version",
            "idempotency_scope",
            "idempotency_key",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        if self.owner_kind not in {"user", "service"}:
            raise ValueError("owner_kind must be user or service")


@dataclass(frozen=True, slots=True)
class DurableJobSpec:
    identity: DurableJobIdentity
    inputs: Any = field(default_factory=dict)
    session_id: str | None = None
    parent_job_id: str | None = None
    parent_fencing_token: int | None = None
    goal_id: str | None = None
    goal_revision: int | None = None
    plan_revision: int | None = None
    candidate_id: str | None = None
    priority: int = 50
    dependencies: tuple[str, ...] = ()
    resource_claims: tuple[str, ...] = ()
    declared_authority: dict[str, Any] = field(default_factory=dict)
    deadline_at: datetime | str | None = None
    max_attempts: int = 1
    service_id: str | None = None


class DurableJobRepository:
    """Persistence operations for the one canonical workflow job record."""

    async def admit_job(self, spec: DurableJobSpec) -> dict[str, Any]:
        identity = spec.identity
        if not spec.declared_authority:
            raise ValueError("declared_authority is required before admission")
        _validate_admission_authority(spec)
        if not 0 <= int(spec.priority) <= 100:
            raise ValueError("priority must be between 0 and 100")
        if int(spec.max_attempts) < 1:
            raise ValueError("max_attempts must be at least 1")
        if identity.job_id in _string_list(spec.dependencies):
            raise DurableJobTransitionError("a durable job cannot depend on itself")
        deadline = _as_utc(spec.deadline_at)
        now = _utc_now()
        input_digest, safe_inputs = _safe_inputs_digest(spec.inputs)
        binding = _binding(
            owner_principal_id=identity.owner_principal_id,
            goal_id=spec.goal_id,
            goal_revision=spec.goal_revision,
            idempotency_scope=identity.idempotency_scope,
            dedupe_key=identity.idempotency_key,
        )
        authority_digest = _digest(spec.declared_authority)
        async with self._session() as db:
            if spec.parent_job_id is not None:
                if spec.parent_fencing_token is None:
                    raise DurableJobLeaseError("parent fencing token is required for child admission")
                try:
                    parent_fence = int(spec.parent_fencing_token)
                except (TypeError, ValueError) as exc:
                    raise DurableJobLeaseError("parent fencing token is malformed") from exc
                if parent_fence <= 0:
                    raise DurableJobLeaseError("parent fencing token is malformed")
                if spec.parent_job_id == identity.job_id:
                    raise DurableJobTransitionError("a durable job cannot parent itself")
                parent = (
                    await db.execute(
                        select(WorkflowRunState).where(
                            WorkflowRunState.run_identity == spec.parent_job_id
                        )
                    )
                ).scalars().first()
                if parent is None:
                    raise DurableJobNotFound(spec.parent_job_id)
                if parent.status != "running":
                    raise DurableJobTransitionError(
                        f"parent job is not running (current={parent.status})"
                    )
                try:
                    parent_expiry = _as_utc(parent.lease_expires_at)
                except ValueError as exc:
                    raise DurableJobLeaseError("parent job lease metadata is malformed") from exc
                if (
                    parent.lease_owner is None
                    or parent_expiry is None
                    or parent_expiry <= now
                    or int(parent.fencing_token or 0) != parent_fence
                ):
                    raise DurableJobLeaseError("parent job fence is stale or expired")
            await ensure_sessions_exist(db, [spec.session_id])
            existing = (
                await db.execute(
                    select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == binding)
                )
            ).scalars().first()
            if existing is not None:
                conflicts = _admission_conflicts(
                    existing,
                    spec=spec,
                    input_digest=input_digest,
                    authority_digest=authority_digest,
                    deadline=deadline,
                )
                if conflicts:
                    raise DurableJobIdempotencyConflict(
                        "idempotency binding conflicts on immutable fields: "
                        + ", ".join(conflicts)
                    )
                db.expunge(existing)
                return _deduped_admission(existing, binding=binding)

            by_id = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity.job_id))
            ).scalars().first()
            if by_id is not None:
                raise DurableJobIdempotencyConflict("job_id already belongs to a different invocation")
            status = "failed" if deadline and deadline <= now else "accepted"
            failure_reason = "deadline_expired" if status == "failed" else None
            run = WorkflowRunState(
                run_identity=identity.job_id,
                root_run_identity=identity.job_id,
                parent_run_identity=spec.parent_job_id,
                parent_job_id=spec.parent_job_id,
                parent_fencing_token=spec.parent_fencing_token,
                workflow_name=identity.job_kind,
                tool_name=identity.job_kind,
                session_id=spec.session_id,
                status=status,
                run_fingerprint=input_digest,
                arguments_json=_canonical(safe_inputs),
                approval_context_json=_canonical(_safe_structure(spec.declared_authority)),
                record_schema_version=DURABLE_JOB_RECORD_SCHEMA_VERSION,
                job_kind=identity.job_kind,
                owner_kind=identity.owner_kind,
                owner_principal_id=identity.owner_principal_id,
                service_id=spec.service_id,
                goal_id=spec.goal_id,
                goal_revision=spec.goal_revision,
                plan_revision=spec.plan_revision,
                candidate_id=spec.candidate_id,
                capability_version=identity.capability_version,
                input_digest=input_digest,
                authority_digest=authority_digest,
                idempotency_scope=identity.idempotency_scope,
                idempotency_key=identity.idempotency_key,
                idempotency_binding=binding,
                priority=int(spec.priority),
                dependencies_json=_canonical(_string_list(spec.dependencies)),
                resource_claims_json=_canonical(_string_list(spec.resource_claims)),
                declared_authority_json=_canonical(_safe_structure(spec.declared_authority)),
                deadline_at=deadline,
                max_attempts=int(spec.max_attempts),
                failure_reason=failure_reason,
                checkpoint_receipts_json="[]",
                artifact_receipts_json="[]",
                effect_receipts_json="[]",
            )
            db.add(run)
            try:
                await db.flush()
            except IntegrityError as exc:
                # The unique binding is the authoritative concurrent-admission
                # fence.  Re-read after rollback so the losing invocation is
                # idempotent when it supplied the same immutable contract, yet
                # still rejects a conflicting job or identity collision.
                await db.rollback()
                existing = (
                    await db.execute(
                        select(WorkflowRunState).where(
                            WorkflowRunState.idempotency_binding == binding
                        )
                    )
                ).scalars().first()
                if existing is not None:
                    conflicts = _admission_conflicts(
                        existing,
                        spec=spec,
                        input_digest=input_digest,
                        authority_digest=authority_digest,
                        deadline=deadline,
                    )
                    if conflicts:
                        raise DurableJobIdempotencyConflict(
                            "idempotency binding conflicts on immutable fields: "
                            + ", ".join(conflicts)
                        ) from exc
                    db.expunge(existing)
                    return _deduped_admission(existing, binding=binding)
                by_id = (
                    await db.execute(
                        select(WorkflowRunState).where(
                            WorkflowRunState.run_identity == identity.job_id
                        )
                    )
                ).scalars().first()
                if by_id is not None:
                    raise DurableJobIdempotencyConflict(
                        "job_id already belongs to a different invocation"
                    ) from exc
                raise DurableJobIdempotencyConflict(
                    "concurrent admission failed before a durable row was visible"
                ) from exc
            db.expunge(run)
            receipt = {
                "kind": "job_admission",
                "status": status,
                "job_id": identity.job_id,
                "idempotency_binding": binding,
                "reason": failure_reason,
                "operator_visible": True,
            }
            return _serialize(run, receipt=receipt)

    async def create_job(self, spec: DurableJobSpec) -> dict[str, Any]:
        """Compatibility alias emphasizing that ScheduledJob is only a trigger."""
        return await self.admit_job(spec)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self._session() as db:
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            ).scalars().first()
            if run is None:
                return None
            db.expunge(run)
            return _serialize(run)

    async def transition_job(
        self,
        job_id: str,
        to_status: str,
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_state: str | None = None,
        expected_status: str | None = None,
        expected_revision: int | None = None,
        expected_fencing_token: int | None = None,
        reason: str | None = None,
        result: Any = None,
        result_summary: str | None = None,
        approval_resume_receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if to_status not in DURABLE_JOB_STATUSES:
            raise DurableJobTransitionError(f"unknown durable job status: {to_status}")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            current = str(run.status)
            if current in DURABLE_JOB_TERMINAL_STATUSES:
                if current == to_status:
                    # Terminal rows deliberately clear their lease.  Replaying
                    # the same terminal command after a crash must therefore
                    # be a side-effect-free no-op even when the caller still
                    # has stale expected revision/owner/fence values.  A
                    # different terminal command remains a typed conflict.
                    db.expunge(run)
                    return _serialize(
                        run,
                        receipt={
                            "kind": "transition",
                            "status": "deduped",
                            "terminal_noop": True,
                            "revision": _revision(run),
                        },
                    )
                raise DurableJobTransitionError(f"terminal job cannot transition {current} -> {to_status}")
            expected_state = expected_state or expected_status
            if expected_state is not None and current != expected_state:
                raise DurableJobTransitionError(
                    f"durable job state changed (expected={expected_state}, current={current})"
                )
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            expected_fence = (
                int(expected_fencing_token)
                if expected_fencing_token is not None
                else (int(fencing_token) if fencing_token is not None else int(run.fencing_token or 0))
            )
            if expected_fencing_token is not None and int(fencing_token or expected_fence) != expected_fence:
                raise DurableJobLeaseError("durable job fencing token is stale")
            if to_status == "queued" and _deadline_expired(run):
                if current == "failed":
                    raise DurableJobTransitionError("job deadline has expired")
                if current in {"accepted", "awaiting_approval", "paused", "blocked"}:
                    # A resume/queue request after the deadline is a durable
                    # failure receipt, never a fresh runnable attempt.
                    to_status = "failed"
                    reason = "deadline_expired"
            effect_ledger: list[dict[str, Any]] | None = None
            approval_resume_record: dict[str, Any] | None = None
            if current == "awaiting_approval" and to_status == "queued":
                if approval_resume_receipt is None:
                    raise DurableJobTransitionError(
                        "illegal approval resume requires a current authenticated approval"
                    )
                approval_resume_record = _validate_approval_resume_receipt(
                    run,
                    approval_resume_receipt,
                    now=_utc_now(),
                )
                effect_ledger = _effect_ledger_or_raise(run.effect_receipts_json)
            if current == "failed" and to_status == "queued":
                raise DurableJobTransitionError(
                    "failed jobs require explicit retry with reconciliation"
                )
            if current == "running" and (owner is None or fencing_token is None):
                raise DurableJobLeaseError(
                    "active jobs require owner and fencing token for every transition"
                )
            if current in {"accepted", "queued", "paused", "blocked"} and to_status in {
                "queued",
                "running",
                "succeeded",
                "cancelled",
            }:
                try:
                    effect_ledger = _effect_ledger_or_raise(run.effect_receipts_json)
                except DurableJobTransitionError:
                    if to_status == "cancelled" and current != "blocked":
                        to_status = "blocked"
                        reason = "malformed_effect_history_requires_reconciliation"
                    else:
                        raise
                if effect_ledger is not None and _job_has_unsafe_effects(effect_ledger):
                    if to_status in {"queued", "running", "succeeded"}:
                        raise DurableJobTransitionError(
                            "unresolved external effect requires exact readback or cost settlement"
                        )
                    if to_status == "cancelled":
                        to_status, recovery_reason = _effect_recovery_state(effect_ledger)
                        reason = reason or f"{recovery_reason}_pending_before_transition"
            if to_status not in DURABLE_JOB_TRANSITIONS.get(current, frozenset()):
                raise DurableJobTransitionError(f"illegal durable job transition {current} -> {to_status}")
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            if current == "running" and to_status in {
                "failed",
                "cancelled",
                "succeeded",
            }:
                try:
                    effect_ledger = _effect_ledger_or_raise(run.effect_receipts_json)
                except DurableJobTransitionError:
                    if to_status == "succeeded":
                        raise
                    to_status = "blocked"
                    reason = "malformed_effect_history_requires_reconciliation"
                if effect_ledger is not None and _job_has_unsafe_effects(effect_ledger):
                    if to_status == "cancelled":
                        to_status, recovery_reason = _effect_recovery_state(effect_ledger)
                        reason = reason or f"{recovery_reason}_pending_before_transition"
            if to_status == "succeeded":
                if _deadline_expired(run):
                    raise DurableJobTransitionError("job deadline has expired")
                effect_ledger = effect_ledger or _effect_ledger_or_raise(run.effect_receipts_json)
                if _job_has_unsafe_effects(effect_ledger):
                    raise DurableJobTransitionError(
                        "cannot mark durable job succeeded: unresolved external effect"
                    )
                if not _verified_readback_exists(effect_ledger):
                    raise DurableJobTransitionError(
                        "cannot mark durable job succeeded: verified capability readback is required"
                    )
            now = _utc_now()
            values: dict[str, Any] = {
                "status": to_status,
                "updated_at": now,
                "heartbeat_at": now,
                "revision": WorkflowRunState.revision + 1,
                "failure_reason": reason if to_status in {
                    "blocked",
                    "failed",
                    *UNCERTAIN_EXTERNAL_EFFECT_STATUSES,
                } else None,
                "finished_at": now if to_status in DURABLE_JOB_TERMINAL_STATUSES or to_status == "failed" else None,
            }
            if to_status in {
                "queued",
                "awaiting_approval",
                "paused",
                "blocked",
                "unknown_external_effect",
                "cost_liability",
                "failed",
                "succeeded",
                "cancelled",
            }:
                values["lease_owner"] = None
                values["lease_expires_at"] = None
            if result is not None:
                values["result_digest"] = _digest(result)
                values["result_summary"] = _text(result_summary, "result recorded")
            elif result_summary is not None:
                values["result_summary"] = _text(result_summary)
            if approval_resume_record is not None:
                values["effect_receipts_json"] = _canonical(
                    _bounded_effect_ledger([*(effect_ledger or []), approval_resume_record])
                )
            conditions = [WorkflowRunState.run_identity == job_id, WorkflowRunState.status == current]
            conditions.append(WorkflowRunState.revision == current_revision)
            if owner is not None:
                conditions.extend(
                    (
                        WorkflowRunState.lease_owner == owner,
                        WorkflowRunState.lease_expires_at > now,
                    )
                )
            if fencing_token is not None:
                conditions.append(WorkflowRunState.fencing_token == fencing_token)
            elif expected_fencing_token is not None:
                conditions.append(WorkflowRunState.fencing_token == expected_fence)
            else:
                conditions.extend(
                    (
                        WorkflowRunState.lease_owner.is_(None),
                        WorkflowRunState.lease_expires_at.is_(None),
                    )
                )
            if to_status not in {"failed", "cancelled"}:
                _append_parent_fence_condition(conditions, run, now=now)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(**values)
            )
            if not _rowcount_is_one(result_update):
                raise DurableJobLeaseError("durable job changed or lease fencing token is stale")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "transition",
                "status": "recorded",
                "from": current,
                "to": to_status,
                "reason": _text(reason) or None,
                "fencing_token": refreshed.fencing_token,
                "revision": _revision(refreshed),
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

    async def queue_job(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self.transition_job(job_id, "queued", **kwargs)

    async def cancel_job(
        self,
        job_id: str,
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_revision: int | None = None,
        reason: str = "operator_cancelled",
    ) -> dict[str, Any]:
        return await self.transition_job(
            job_id,
            "cancelled",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
            reason=reason,
        )

    async def pause_job(
        self,
        job_id: str,
        *,
        owner: str,
        fencing_token: int,
        reason: str = "operator_paused",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Pause a leased job through the canonical transition/CAS path."""
        return await self.transition_job(
            job_id,
            "paused",
            owner=owner,
            fencing_token=fencing_token,
            reason=reason,
            expected_revision=expected_revision,
        )

    async def resume_job(
        self,
        job_id: str,
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_revision: int | None = None,
        reason: str = "operator_resumed",
    ) -> dict[str, Any]:
        """Resume a paused/approval-held job into the bounded queue."""
        return await self.transition_job(
            job_id,
            "queued",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
            reason=reason,
        )

    async def resume_approved_job(
        self,
        job_id: str,
        *,
        approval_receipt: Mapping[str, Any],
        approval_id: str,
        authority_digest: str,
        goal_id: str | None,
        goal_revision: int | None,
        plan_revision: int | None,
        capability_version: str,
        owner_kind: str,
        owner_principal_id: str,
        service_id: str | None,
        budget_microusd: int | None,
        budget_digest: str,
        operator_principal_id: str,
        operator_session_id: str,
        expires_at: float,
        expected_revision: int | None = None,
        reason: str = "operator_approval_resumed",
    ) -> dict[str, Any]:
        """Resume approval-held work through the current authority binding.

        ``resume_job`` deliberately has no approval capability.  Callers must
        use this typed seam with a fresh authenticated approval receipt.  All
        immutable owner, authority, goal/plan, and budget fields are explicit
        so an adapter cannot accidentally resume from a stale projection.
        """
        if not isinstance(approval_receipt, Mapping):
            raise DurableJobTransitionError("approval resume requires a typed approval receipt")
        receipt_fields = dict(approval_receipt)
        if any(
            field_name in receipt_fields and receipt_fields[field_name] != expected
            for field_name, expected in {
                "approval_id": approval_id,
                "authority_digest": authority_digest,
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "plan_revision": plan_revision,
                "capability_version": capability_version,
                "owner_kind": owner_kind,
                "owner_principal_id": owner_principal_id,
                "service_id": service_id,
                "budget_microusd": budget_microusd,
                "budget_digest": budget_digest,
                "operator_principal_id": operator_principal_id,
                "operator_session_id": operator_session_id,
                "expires_at": expires_at,
            }.items()
        ):
            raise DurableJobTransitionError("approval resume receipt does not match its explicit binding")
        approval_resume_receipt = {
            **receipt_fields,
            "status": receipt_fields.get("status"),
            "authenticated": receipt_fields.get("authenticated"),
            "operator_principal_id": operator_principal_id,
            "operator_session_id": operator_session_id,
            "owner_kind": owner_kind,
            "owner_principal_id": owner_principal_id,
            "service_id": service_id,
            "approval_id": approval_id,
            "authority_digest": authority_digest,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "plan_revision": plan_revision,
            "capability_version": capability_version,
            "budget_microusd": budget_microusd,
            "budget_digest": budget_digest,
            "expires_at": expires_at,
        }
        return await self.transition_job(
            job_id,
            "queued",
            expected_state="awaiting_approval",
            expected_revision=expected_revision,
            reason=reason,
            approval_resume_receipt=approval_resume_receipt,
        )

    async def revoke_job(
        self,
        job_id: str,
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_revision: int | None = None,
        reason: str = "operator_revoked",
    ) -> dict[str, Any]:
        """Use the terminal cancellation transition for an authority revoke."""
        return await self.transition_job(
            job_id,
            "cancelled",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
            reason=reason,
        )

    async def fail_unclaimed_job(
        self,
        job_id: str,
        *,
        owner_principal_id: str,
        service_id: str,
        reason: str,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        """Fail an admitted job before lease acquisition with an atomic owner fence.

        Admission and queue failures happen before a runner lease exists. This
        narrow path is still conditional on the authenticated service owner and
        accepted/queued status, so a competing claim or authority change wins
        the race instead of being overwritten by an ownerless transition.
        """
        _validate_owner_fields(
            owner_kind="service",
            owner_principal_id=owner_principal_id,
            service_id=service_id,
        )
        async with self._session() as db:
            now = _utc_now()
            current = await self._fetch(db, job_id)
            expected_revision = _revision(current)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status.in_(("accepted", "queued")),
                    WorkflowRunState.revision == expected_revision,
                    WorkflowRunState.owner_kind == "service",
                    WorkflowRunState.owner_principal_id == owner_principal_id,
                    WorkflowRunState.service_id == service_id,
                )
                .values(
                    status="failed",
                    failure_reason=_text(reason, "unclaimed_job_failed"),
                    result_summary=result_summary,
                    updated_at=now,
                    heartbeat_at=now,
                    finished_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                current = await self._fetch(db, job_id)
                db.expunge(current)
                return _serialize(
                    current,
                    receipt={
                        "kind": "unclaimed_failure",
                        "status": "not_recorded",
                        "reason": "job_changed_before_owner_fence",
                        "operator_visible": True,
                    },
                )
            failed = await self._fetch(db, job_id)
            receipt = {
                "kind": "unclaimed_failure",
                "status": "recorded",
                "reason": _text(reason, "unclaimed_job_failed"),
                "owner_principal_id": owner_principal_id,
                "service_id": service_id,
                "operator_visible": True,
            }
            db.expunge(failed)
            return _serialize(failed, receipt=receipt)

    async def _dependency_outcome(
        self,
        db: Any,
        run: WorkflowRunState,
    ) -> tuple[str, str | None, str | None, str | None]:
        """Classify dependencies before a runner lease is admitted.

        A successful dependency is satisfied.  A missing/failed/cancelled
        dependency makes the child permanently ineligible for this attempt;
        an uncertain dependency needs reconciliation first; all other live
        states remain pending.  The caller performs the state change with its
        own revision/fence CAS.
        """
        dependencies = _dependency_ids(run)
        if not dependencies:
            return "ready", None, None, None
        result = await db.execute(
            select(WorkflowRunState).where(WorkflowRunState.run_identity.in_(dependencies))
        )
        by_id = {item.run_identity: item for item in result.scalars().all()}
        for dependency_id in dependencies:
            dependency = by_id.get(dependency_id)
            if dependency is None:
                return "failed", "dependency_missing", dependency_id, None
            dependency_status = _text(dependency.status)
            if dependency_status in {"succeeded", "completed"}:
                continue
            if dependency_status in DEPENDENCY_FAILURE_STATUSES:
                return "failed", "dependency_failed", dependency_id, dependency_status
            if dependency_status in DEPENDENCY_UNRESOLVED_STATUSES:
                return "blocked", "dependency_requires_reconciliation", dependency_id, dependency_status
            return "pending", "dependency_pending", dependency_id, dependency_status
        return "ready", None, None, None

    async def claim_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_seconds: int = 300,
        expected_state: str = "queued",
        expected_revision: int | None = None,
        expected_fencing_token: int | None = None,
    ) -> dict[str, Any]:
        owner = _text(owner)
        if not owner:
            raise DurableJobLeaseError("owner is required to claim a job")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        expires = now + timedelta(seconds=int(lease_seconds))
        if expected_state != "queued":
            raise DurableJobTransitionError("durable job claims require the queued state")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status in DURABLE_JOB_TERMINAL_STATUSES:
                db.expunge(run)
                return _serialize(run, receipt={"kind": "claim", "status": "terminal_noop"})
            if run.status != expected_state:
                raise DurableJobTransitionError(
                    f"only {expected_state} jobs may be claimed (current={run.status})"
                )
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            expected_fence = (
                int(expected_fencing_token)
                if expected_fencing_token is not None
                else int(run.fencing_token or 0)
            )
            if int(run.fencing_token or 0) != expected_fence:
                raise DurableJobLeaseError("durable job fencing token is stale")
            try:
                effect_ledger = _effect_ledger_or_raise(run.effect_receipts_json)
            except DurableJobTransitionError:
                malformed_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                    or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= now),
                ]
                _append_parent_fence_condition(malformed_conditions, run, now=now)
                malformed = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(*malformed_conditions)
                    .values(
                        status="blocked",
                        failure_reason="malformed_effect_history_requires_reconciliation",
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                        heartbeat_at=now,
                        finished_at=None,
                        revision=WorkflowRunState.revision + 1,
                    )
                )
                if not _rowcount_is_one(malformed):
                    raise DurableJobLeaseError("job changed before malformed effect history was blocked")
                blocked_job = await self._fetch(db, job_id)
                db.expunge(blocked_job)
                return _serialize(
                    blocked_job,
                    receipt={
                        "kind": "claim",
                        "status": "blocked",
                        "reason": "malformed_effect_history_requires_reconciliation",
                        "revision": _revision(blocked_job),
                        "operator_visible": True,
                    },
                )
            if _job_has_unsafe_effects(effect_ledger):
                recovery_status, recovery_reason = _effect_recovery_state(effect_ledger)
                recovery_reason = f"queued_{recovery_reason}_requires_reconciliation"
                recovery_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                    or_(WorkflowRunState.lease_owner.is_(None), WorkflowRunState.lease_expires_at <= now),
                ]
                _append_parent_fence_condition(recovery_conditions, run, now=now)
                recovered = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(*recovery_conditions)
                    .values(
                        status=recovery_status,
                        failure_reason=recovery_reason,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                        heartbeat_at=now,
                        finished_at=None,
                        revision=WorkflowRunState.revision + 1,
                    )
                )
                if not _rowcount_is_one(recovered):
                    raise DurableJobLeaseError("job changed before unresolved effect recovery")
                recovered_job = await self._fetch(db, job_id)
                receipt = {
                    "kind": "claim",
                    "status": "blocked",
                    "reason": recovery_reason,
                    "recovery_state": recovery_status,
                    "revision": _revision(recovered_job),
                    "operator_action": "reconcile_external_effect_before_claim_or_retry",
                    "operator_visible": True,
                }
                db.expunge(recovered_job)
                return _serialize(recovered_job, receipt=receipt)
            try:
                persisted_deadline = _as_utc(run.deadline_at)
            except ValueError as exc:
                raise DurableJobTransitionError("job deadline metadata is malformed") from exc
            if persisted_deadline and persisted_deadline <= now:
                deadline_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                ]
                _append_parent_fence_condition(deadline_conditions, run, now=now)
                expired = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(*deadline_conditions)
                    .values(
                        status="failed",
                        failure_reason="deadline_expired",
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                        heartbeat_at=now,
                        finished_at=now,
                        revision=WorkflowRunState.revision + 1,
                    )
                )
                if not _rowcount_is_one(expired):
                    raise DurableJobLeaseError("job changed before deadline transition")
                failed = await self._fetch(db, job_id)
                db.expunge(failed)
                return _serialize(
                    failed,
                    receipt={
                        "kind": "claim",
                        "status": "failed",
                        "reason": "deadline_expired",
                        "revision": _revision(failed),
                        "operator_visible": True,
                    },
                )
            dependency_state, dependency_reason, dependency_id, dependency_status = await self._dependency_outcome(
                db, run
            )
            if dependency_state == "failed":
                dependency_failure_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                    or_(
                        WorkflowRunState.lease_owner.is_(None),
                        WorkflowRunState.lease_expires_at <= now,
                    ),
                ]
                _append_parent_fence_condition(dependency_failure_conditions, run, now=now)
                failed = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(*dependency_failure_conditions)
                    .values(
                        status="failed",
                        failure_reason=dependency_reason,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                        heartbeat_at=now,
                        finished_at=now,
                        revision=WorkflowRunState.revision + 1,
                    )
                )
                if not _rowcount_is_one(failed):
                    raise DurableJobLeaseError("job changed before dependency failure transition")
                failed_job = await self._fetch(db, job_id)
                receipt = {
                    "kind": "claim",
                    "status": "failed",
                    "reason": dependency_reason,
                    "dependency_id": dependency_id,
                    "dependency_status": dependency_status,
                    "revision": _revision(failed_job),
                    "operator_visible": True,
                }
                db.expunge(failed_job)
                return _serialize(failed_job, receipt=receipt)
            if dependency_state == "blocked":
                dependency_block_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                    or_(
                        WorkflowRunState.lease_owner.is_(None),
                        WorkflowRunState.lease_expires_at <= now,
                    ),
                ]
                _append_parent_fence_condition(dependency_block_conditions, run, now=now)
                blocked = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(*dependency_block_conditions)
                    .values(
                        status="blocked",
                        failure_reason=dependency_reason,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                        heartbeat_at=now,
                        finished_at=None,
                        revision=WorkflowRunState.revision + 1,
                    )
                )
                if not _rowcount_is_one(blocked):
                    raise DurableJobLeaseError("job changed before dependency recovery block")
                blocked_job = await self._fetch(db, job_id)
                receipt = {
                    "kind": "claim",
                    "status": "blocked",
                    "reason": dependency_reason,
                    "dependency_id": dependency_id,
                    "dependency_status": dependency_status,
                    "revision": _revision(blocked_job),
                    "operator_visible": True,
                }
                db.expunge(blocked_job)
                return _serialize(blocked_job, receipt=receipt)
            if dependency_state == "pending":
                db.expunge(run)
                return _serialize(
                    run,
                    receipt={
                        "kind": "claim",
                        "status": "blocked",
                        "reason": dependency_reason,
                        "dependency_id": dependency_id,
                        "dependency_status": dependency_status,
                        "state_unchanged": True,
                        "operator_visible": True,
                    },
                )
            if run.attempt_count >= run.max_attempts:
                raise DurableJobTransitionError("attempt budget exhausted")
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == expected_state,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.fencing_token == expected_fence,
                or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= now),
            ]
            _append_parent_fence_condition(conditions, run, now=now)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    status="running",
                    lease_owner=owner,
                    lease_expires_at=expires,
                    fencing_token=WorkflowRunState.fencing_token + 1,
                    revision=WorkflowRunState.revision + 1,
                    attempt_count=WorkflowRunState.attempt_count + 1,
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            if not _rowcount_is_one(result_update):
                raise DurableJobLeaseError("job is currently owned by another active runner")
            claimed = await self._fetch(db, job_id)
            receipt = {
                "kind": "claim",
                "status": "claimed",
                "owner": owner,
                "fencing_token": claimed.fencing_token,
                "lease_expires_at": expires.isoformat(),
                "attempt": claimed.attempt_count,
                "revision": _revision(claimed),
                "operator_visible": True,
            }
            db.expunge(claimed)
            return _serialize(claimed, receipt=receipt)

    async def heartbeat_job(
        self,
        job_id: str,
        *,
        owner: str,
        fencing_token: int,
        lease_seconds: int | None = None,
        expected_state: str = "running",
        expected_revision: int | None = None,
        expected_fencing_token: int | None = None,
    ) -> dict[str, Any]:
        """Refresh a live lease with a row-level state/revision/fence CAS.

        The conditional update is the authority. A worker that lost its lease
        or raced a recovery write receives a lease error and cannot make the
        stale row look live again.
        """
        owner = _text(owner)
        if not owner or fencing_token is None:
            raise DurableJobLeaseError("owner and fencing token are required for heartbeat")
        if lease_seconds is not None and int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            current_revision = _revision(run)
            if run.status != expected_state:
                raise DurableJobTransitionError(
                    f"heartbeat requires {expected_state} state (current={run.status})"
                )
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            expected_fence = (
                int(expected_fencing_token)
                if expected_fencing_token is not None
                else int(fencing_token)
            )
            if int(fencing_token) != expected_fence:
                raise DurableJobLeaseError("durable job fencing token is stale")
            if run.lease_owner != owner or int(run.fencing_token or 0) != expected_fence:
                raise DurableJobLeaseError("active owner lease and fencing token are required")
            try:
                persisted_expiry = _as_utc(run.lease_expires_at)
            except ValueError as exc:
                raise DurableJobLeaseError("job lease metadata is malformed") from exc
            if persisted_expiry is None or persisted_expiry <= now:
                raise DurableJobLeaseError("job lease has expired")
            if _deadline_expired(run, now=now):
                deadline_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.lease_owner == owner,
                    WorkflowRunState.fencing_token == expected_fence,
                    WorkflowRunState.lease_expires_at > now,
                ]
                _append_parent_fence_condition(deadline_conditions, run, now=now)
                expired = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(*deadline_conditions)
                    .values(
                        status="failed",
                        failure_reason="deadline_expired",
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=now,
                        updated_at=now,
                        heartbeat_at=now,
                        revision=WorkflowRunState.revision + 1,
                    )
                )
                if not _rowcount_is_one(expired):
                    raise DurableJobLeaseError("job changed before deadline transition")
                failed = await self._fetch(db, job_id)
                receipt = {
                    "kind": "heartbeat",
                    "status": "failed",
                    "reason": "deadline_expired",
                    "owner": owner,
                    "fencing_token": failed.fencing_token,
                    "revision": _revision(failed),
                    "operator_visible": True,
                }
                db.expunge(failed)
                return _serialize(failed, receipt=receipt)
            expires = (
                now + timedelta(seconds=int(lease_seconds))
                if lease_seconds is not None
                else persisted_expiry
            )
            heartbeat_conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == expected_state,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.lease_owner == owner,
                WorkflowRunState.fencing_token == expected_fence,
                WorkflowRunState.lease_expires_at > now,
            ]
            _append_parent_fence_condition(heartbeat_conditions, run, now=now)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*heartbeat_conditions)
                .values(
                    lease_expires_at=expires,
                    heartbeat_at=now,
                    updated_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("job changed or lease fencing token is stale")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "heartbeat",
                "status": "recorded",
                "owner": owner,
                "fencing_token": refreshed.fencing_token,
                "revision": _revision(refreshed),
                "lease_expires_at": refreshed.lease_expires_at.isoformat()
                if refreshed.lease_expires_at
                else None,
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

    # Keep the shorter name available to scheduler/workflow adapters.
    heartbeat = heartbeat_job

    async def transfer_lease(
        self,
        job_id: str,
        *,
        owner: str | None = None,
        to_owner: str | None = None,
        from_owner: str | None = None,
        expected_owner: str | None = None,
        fencing_token: int | None = None,
        expected_fencing_token: int | None = None,
        expected_revision: int | None = None,
        lease_seconds: int = 300,
        expected_state: str = "running",
    ) -> dict[str, Any]:
        """Transfer an expired lease under an explicit recovery fence."""
        new_owner = _text(to_owner or owner)
        old_owner = _text(from_owner or expected_owner)
        if not new_owner or not old_owner or new_owner == old_owner:
            raise DurableJobLeaseError("lease transfer requires distinct source and destination owners")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            current_revision = _revision(run)
            if run.status != expected_state:
                raise DurableJobTransitionError(
                    f"lease transfer requires {expected_state} state (current={run.status})"
                )
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            current_fence = int(run.fencing_token or 0)
            expected_fence = (
                int(expected_fencing_token)
                if expected_fencing_token is not None
                else (int(fencing_token) if fencing_token is not None else current_fence)
            )
            if current_fence != expected_fence:
                raise DurableJobLeaseError("durable job fencing token is stale")
            try:
                expiry = _as_utc(run.lease_expires_at)
            except ValueError as exc:
                raise DurableJobLeaseError("job lease metadata is malformed") from exc
            if run.lease_owner != old_owner:
                raise DurableJobLeaseError("lease source owner does not match")
            if expiry is not None and expiry > now:
                raise DurableJobLeaseError("active lease cannot be transferred before expiry")
            transfer_conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == expected_state,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.lease_owner == old_owner,
                WorkflowRunState.fencing_token == expected_fence,
                or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= now),
            ]
            _append_parent_fence_condition(transfer_conditions, run, now=now)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*transfer_conditions)
                .values(
                    lease_owner=new_owner,
                    lease_expires_at=now + timedelta(seconds=int(lease_seconds)),
                    fencing_token=WorkflowRunState.fencing_token + 1,
                    revision=WorkflowRunState.revision + 1,
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("job changed before lease transfer")
            transferred = await self._fetch(db, job_id)
            receipt = {
                "kind": "lease_transfer",
                "status": "recorded",
                "previous_owner": old_owner,
                "owner": new_owner,
                "fencing_token": transferred.fencing_token,
                "revision": _revision(transferred),
                "lease_expires_at": transferred.lease_expires_at.isoformat()
                if transferred.lease_expires_at
                else None,
                "operator_visible": True,
            }
            db.expunge(transferred)
            return _serialize(transferred, receipt=receipt)

    async def record_checkpoint(
        self,
        job_id: str,
        *,
        checkpoint_id: str,
        state: Any,
        owner: str,
        fencing_token: int,
        safe: bool = True,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not _text(checkpoint_id):
            raise ValueError("checkpoint_id is required")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if _deadline_expired(run):
                raise DurableJobTransitionError("job deadline has expired")
            if owner is None or fencing_token is None:
                raise DurableJobLeaseError("owner and fencing token are required for checkpoint writes")
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            if run.status not in {"running", "paused", "awaiting_approval"}:
                raise DurableJobTransitionError(f"checkpoint not allowed from {run.status}")
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            state_digest = _digest(state)
            receipt = {
                "checkpoint_id": checkpoint_id,
                "state_digest": state_digest,
                "state_keys": sorted(str(key) for key in state.keys()) if isinstance(state, dict) else [],
                "safe": bool(safe),
                "recorded_at": _utc_now().isoformat(),
                "fencing_token": fencing_token,
            }
            existing = _json_load(run.checkpoint_receipts_json, [])
            existing = [item for item in existing if isinstance(item, dict) and item.get("checkpoint_id") != checkpoint_id]
            existing.append(receipt)
            now = _utc_now()
            checkpoint_conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.fencing_token == fencing_token,
                WorkflowRunState.lease_owner == owner,
                WorkflowRunState.lease_expires_at > now,
            ]
            _append_parent_fence_condition(checkpoint_conditions, run, now=now)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*checkpoint_conditions)
                .values(
                    checkpoint_receipts_json=_canonical(existing[-50:]),
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(result_update):
                raise DurableJobLeaseError("stale job fencing token")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "checkpoint", "status": "recorded", **receipt})

    async def record_artifact(
        self,
        job_id: str,
        *,
        file_path: str,
        artifact_type: str = "workspace_file",
        content: str | bytes | None = None,
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist an operator-safe artifact receipt.

        An ownerless, content-free receipt is allowed only in ``accepted``
        before execution is claimed. A leased/running job must supply the
        current lease owner and fencing token so a stale runner cannot append
        an artifact after restart recovery.
        """
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if _deadline_expired(run):
                raise DurableJobTransitionError("job deadline has expired")
            if run.status in DURABLE_JOB_TERMINAL_STATUSES:
                raise DurableJobTransitionError(f"terminal job cannot record artifacts ({run.status})")
            lease_present = bool(run.lease_owner or run.lease_expires_at)
            if lease_present or run.status == "running":
                if owner is None or fencing_token is None:
                    raise DurableJobLeaseError("owner and fencing token are required for leased artifact writes")
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            elif owner is None and fencing_token is None:
                # The only ownerless path is a pre-execution receipt with no
                # content mutation.  Queued/failed/blocked rows need an
                # explicit owner even when they currently have no lease.
                if run.status != "accepted" or content is not None:
                    raise DurableJobLeaseError(
                        "owner and fencing token are required to alter artifact evidence"
                    )
            else:
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            record = build_artifact_record(
                file_path=file_path,
                artifact_type=artifact_type,
                producer=run.job_kind,
                run_id=job_id,
                session_id=run.session_id,
                content=content,
            )
            receipt = {
                "artifact_id": record["artifact_id"],
                "artifact_type": record["artifact_type"],
                "file_path": record["file_path"],
                "producer": record["producer"],
                "content_sha256": record["content_sha256"],
                "size_bytes": record["size_bytes"],
                "exists": record["exists"],
                "recorded_at": _utc_now().isoformat(),
            }
            existing = _json_load(run.artifact_receipts_json, [])
            existing = [item for item in existing if isinstance(item, dict) and item.get("artifact_id") != receipt["artifact_id"]]
            existing.append(receipt)
            now = _utc_now()
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
            ]
            if owner is not None:
                conditions.extend(
                    (
                        WorkflowRunState.lease_owner == owner,
                        WorkflowRunState.fencing_token == fencing_token,
                        WorkflowRunState.lease_expires_at > now,
                    )
                )
            else:
                conditions.extend((WorkflowRunState.lease_owner.is_(None), WorkflowRunState.lease_expires_at.is_(None)))
            _append_parent_fence_condition(conditions, run, now=now)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    artifact_receipts_json=_canonical(existing[-100:]),
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(result_update):
                raise DurableJobLeaseError("stale job fencing token")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "artifact", "status": "recorded", **receipt})

    async def record_effect(
        self,
        job_id: str,
        *,
        effect_type: str,
        effect_id: str | None = None,
        target_path: str | None = None,
        target_digest: str | None = None,
        approval_id: str | None = None,
        adapter_idempotency_key: str | None = None,
        status: str = "succeeded",
        content_sha256: str | None = None,
        details: dict[str, Any] | None = None,
        receipt_kind: str = "effect",
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist a bounded effect or readback receipt on the job record.

        The effect ledger is intentionally part of the canonical durable job
        row.  A running job must use its current lease and fencing token, so a
        stale runner cannot report an external write or verification after
        restart recovery.  ``details`` is structural and redacted before it
        is persisted; callers should pass digests rather than content.
        """
        effect_type = _text(effect_type)
        receipt_kind = _text(receipt_kind, "effect")
        status = _text(status, "unknown")
        if not effect_type:
            raise ValueError("effect_type is required")
        if receipt_kind not in {"effect", "readback"}:
            raise ValueError("receipt_kind must be effect or readback")
        if status not in {"succeeded", "failed", "unknown", "blocked", "intent", "dispatched"}:
            raise ValueError("effect status is not supported")
        effect_id = _text(effect_id) or None
        target_path = _text(target_path) or None
        target_digest = _text(target_digest) or None
        approval_id = _text(approval_id) or None
        adapter_idempotency_key = _text(adapter_idempotency_key) or None
        if content_sha256 is not None and not _text(content_sha256):
            content_sha256 = None
        safe_details = _safe_structure(details or {})
        if effect_id is None:
            # The lifecycle status and receipt payload are observations of one
            # effect.  They must not create a new identity on every update or
            # an old ``intent`` would remain unresolved after ``succeeded``.
            effect_id = "eff_" + _digest({
                "job_id": job_id,
                "receipt_kind": receipt_kind,
                "effect_type": effect_type,
                "target_path": target_path or "",
                "target_digest": target_digest or "",
                "adapter_idempotency_key": adapter_idempotency_key or "",
            })[:24]
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if _deadline_expired(run):
                raise DurableJobTransitionError("job deadline has expired")
            if run.status in DURABLE_JOB_TERMINAL_STATUSES:
                raise DurableJobTransitionError(f"terminal job cannot record effects ({run.status})")
            lease_present = bool(run.lease_owner or run.lease_expires_at)
            if run.status == "running" and (owner is None or fencing_token is None):
                raise DurableJobLeaseError("active jobs require owner and fencing token for effect writes")
            if lease_present or run.status == "running":
                if owner is None or fencing_token is None:
                    raise DurableJobLeaseError("owner and fencing token are required for leased effect writes")
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            elif owner is None and fencing_token is None:
                if run.status != "accepted":
                    raise DurableJobLeaseError("owner and fencing token are required to alter effect evidence")
            else:
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            receipt = {
                "effect_id": effect_id,
                "receipt_kind": receipt_kind,
                "effect_type": effect_type,
                "target_path": target_path,
                "target_digest": target_digest,
                "approval_id": approval_id,
                "adapter_idempotency_key": adapter_idempotency_key,
                "status": status,
                "content_sha256": content_sha256,
                "details": safe_details,
                "recorded_at": _utc_now().isoformat(),
                "fencing_token": fencing_token,
            }
            existing = _effect_ledger_or_raise(run.effect_receipts_json)
            previous = next(
                (
                    item
                    for item in existing
                    if isinstance(item, dict) and item.get("effect_id") == effect_id
                ),
                None,
            )
            if previous is not None and (
                _text(previous.get("effect_type")) != effect_type
                or _text(previous.get("target_path")) != _text(target_path)
                or any(
                    _text(previous.get(field_name))
                    and _text(value)
                    and _text(previous.get(field_name)) != _text(value)
                    for field_name, value in (
                        ("target_digest", target_digest),
                        ("approval_id", approval_id),
                        ("adapter_idempotency_key", adapter_idempotency_key),
                    )
                )
            ):
                raise DurableJobIdempotencyConflict(
                    "effect_id already identifies a different effect target"
                )
            previous_status = _text(previous.get("status")) if previous is not None else ""
            if previous_status in UNRESOLVED_EFFECT_STATUSES:
                if receipt_kind == "effect" and status not in UNRESOLVED_EFFECT_STATUSES:
                    raise DurableJobTransitionError(
                        "unresolved external effect requires exact readback or cost settlement"
                    )
                if receipt_kind == "effect":
                    lifecycle_order = {"unknown": 0, "intent": 1, "dispatched": 2}
                    # A post-dispatch transport observation may be reported as
                    # ``unknown`` after an intent was durably recorded.  It
                    # remains unresolved, but is not a lifecycle rollback.
                    intent_to_unknown = previous_status == "intent" and status == "unknown"
                    if (
                        not intent_to_unknown
                        and lifecycle_order.get(status, -1) < lifecycle_order.get(previous_status, 0)
                    ):
                        raise DurableJobTransitionError(
                            "unresolved external effect lifecycle cannot move backwards"
                        )
                elif not _text(target_path) or _text(target_path) != _text(previous.get("target_path")):
                    raise DurableJobTransitionError(
                        "readback must identify the exact unresolved effect target"
                    )
                elif (
                    _text(previous.get("target_digest"))
                    and _text(target_digest)
                    and _text(previous.get("target_digest")) != _text(target_digest)
                ):
                    raise DurableJobIdempotencyConflict(
                        "readback target digest does not match the intended effect"
                    )
            if receipt_kind == "readback" and status == "succeeded":
                if not _verified_readback_exists([receipt]):
                    raise DurableJobTransitionError(
                        "successful readback requires verified capability evidence"
                    )
                receipt["reconciled"] = True
                receipt["reconciliation_status"] = "resolved"
            preserve_unresolved = (
                receipt_kind == "readback"
                and previous_status in UNRESOLVED_EFFECT_STATUSES
                and status != "succeeded"
            )
            if preserve_unresolved:
                # A failed/blocked/unknown readback is an observation about
                # verification, not proof that the intended effect was absent.
                # Keep the original intent/dispatched liability addressable by
                # reconciliation and retain this bounded diagnostic separately.
                receipt["original_effect_id"] = effect_id
                receipt["effect_id"] = (
                    f"{effect_id}:readback:{_digest({'status': status, 'target_path': target_path})[:16]}"
                )
                receipt["details"] = {
                    **(safe_details if isinstance(safe_details, dict) else {}),
                    "readback_observation_only": True,
                    "verified": False,
                }
                existing = [item for item in existing if not (
                    isinstance(item, dict) and item.get("effect_id") == receipt["effect_id"]
                )]
            else:
                existing = [
                    item
                    for item in existing
                    if isinstance(item, dict) and item.get("effect_id") != effect_id
                ]
            existing.append(receipt)
            now = _utc_now()
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
            ]
            if owner is not None:
                conditions.extend(
                    (
                        WorkflowRunState.lease_owner == owner,
                        WorkflowRunState.fencing_token == fencing_token,
                        WorkflowRunState.lease_expires_at > now,
                    )
                )
            else:
                conditions.extend((WorkflowRunState.lease_owner.is_(None), WorkflowRunState.lease_expires_at.is_(None)))
            _append_parent_fence_condition(conditions, run, now=now)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    effect_receipts_json=_canonical(_bounded_effect_ledger(existing)),
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(result_update):
                raise DurableJobLeaseError("stale job fencing token")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": receipt_kind, "status": "recorded", **receipt})

    async def record_readback(
        self,
        job_id: str,
        *,
        target_path: str,
        status: str,
        effect_id: str | None = None,
        effect_type: str | None = None,
        target_digest: str | None = None,
        content_sha256: str | None = None,
        details: dict[str, Any] | None = None,
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Record an explicit readback receipt in the canonical effect ledger."""
        if effect_id and effect_type is None:
            current = await self.get_job(job_id)
            if current is not None:
                prior = next(
                    (
                        item
                        for item in current.get("effects", [])
                        if isinstance(item, dict) and item.get("effect_id") == effect_id
                    ),
                    None,
                )
                if prior is not None:
                    effect_type = _text(prior.get("effect_type"), "readback")
        return await self.record_effect(
            job_id,
            effect_type=effect_type or "readback",
            effect_id=effect_id,
            receipt_kind="readback",
            target_path=target_path,
            target_digest=target_digest,
            status=status,
            content_sha256=content_sha256,
            details=details,
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
        )

    async def record_remote_inference_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        owner: str | None = None,
        fencing_token: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist one redacted remote admission receipt on an existing job.

        This is an explicit caller-adoption seam for the process-local remote
        broker.  It records the broker status as an effect receipt and leaves
        the canonical job lifecycle under the existing job methods.  In
        particular, an uncertain ``blocked`` receipt cannot release or retry
        the remote lease, and a queued/cancelled/expired receipt cannot invoke
        a provider callback through this method.

        A durable job row is required; this method never creates one.  Active
        durable jobs must provide their existing owner/fencing pair, while an
        ``accepted`` row may record an admission receipt before it is claimed.
        """
        safe_receipt, receipt_digest = _canonical_remote_inference_receipt(receipt)
        job_id = str(safe_receipt["job_id"])
        current = await self.get_job(job_id)
        if current is None:
            raise DurableJobNotFound(job_id)
        persisted_owner = str(current["owner"].get("principal_id") or "")
        if persisted_owner != str(safe_receipt["owner_id"]):
            raise DurableJobLeaseError("remote inference receipt owner does not match the durable job owner")
        admission_status = str(safe_receipt["status"])
        return await self.record_effect(
            job_id,
            effect_type="remote_inference_admission",
            effect_id=f"remote_inference:{safe_receipt['operation_id']}",
            target_path=f"remote_inference:{safe_receipt['operation_id']}",
            target_digest=_text(safe_receipt.get("operation_id")) or None,
            adapter_idempotency_key=_text(safe_receipt.get("operation_id")) or None,
            status=REMOTE_INFERENCE_EFFECT_STATUSES[admission_status],
            details={
                "admission_status": admission_status,
                "receipt": safe_receipt,
                "receipt_digest": receipt_digest,
            },
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
        )

    async def retry_job(
        self,
        job_id: str,
        *,
        owner_kind: str,
        owner_principal_id: str,
        service_id: str | None = None,
        reconciliation_receipt: Any,
        reconciled: bool | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if reconciled is False:
            raise DurableJobTransitionError("failed jobs require external-effect reconciliation before retry")
        canonical_receipt, receipt_digest = _canonical_reconciliation_receipt(reconciliation_receipt)
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES:
                raise DurableJobTransitionError(
                    f"{run.status} requires explicit reconciliation before retry"
                )
            if run.status != "failed":
                raise DurableJobTransitionError(f"only failed jobs may be retried (current={run.status})")
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            _validate_retry_actor(
                run,
                owner_kind=owner_kind,
                owner_principal_id=owner_principal_id,
                service_id=service_id,
            )
            if int(run.attempt_count or 0) >= int(run.max_attempts or 1):
                raise DurableJobTransitionError("attempt budget exhausted")
            if _deadline_expired(run):
                raise DurableJobTransitionError("job deadline has expired")
            existing_effects = _effect_ledger_or_raise(run.effect_receipts_json)
            failure_reason = _text(run.failure_reason)
            if not existing_effects and failure_reason in EFFECT_RECONCILIATION_REQUIRED_REASONS:
                raise DurableJobTransitionError(
                    "durable effect history is missing for an effect-bound failure; reconciliation is required"
                )
            if _job_has_unsafe_effects(existing_effects) or _text(run.failure_reason) in UNSAFE_RETRY_REASONS:
                raise DurableJobTransitionError(
                    "failed job retains unknown external effect or cost liability; reconcile before retry"
                )
            receipt_payload = _json_load(canonical_receipt, {})
            matched_effect: Mapping[str, Any] | None = None
            if existing_effects:
                for item in existing_effects:
                    if (
                        isinstance(item, Mapping)
                        and _text(item.get("effect_id")) == _text(receipt_payload.get("effect_id"))
                        and _text(item.get("effect_type")) == _text(receipt_payload.get("effect_type"))
                    ):
                        matched_effect = item
                        break
                if matched_effect is None:
                    raise DurableJobTransitionError(
                        "retry reconciliation receipt does not match a durable effect"
                    )
                _reconciliation_matches_effect(matched_effect, receipt_payload)
            else:
                _validate_no_effect_retry_receipt(job_id, receipt_payload)
            existing_effects.append(
                {
                    "kind": "reconciliation",
                    "status": "reconciled",
                    "receipt": _json_load(canonical_receipt, {}),
                    "receipt_digest": receipt_digest,
                    "owner_kind": owner_kind,
                    "owner_principal_id": owner_principal_id,
                    "service_id": service_id,
                    "recorded_at": _utc_now().isoformat(),
                }
            )
            now = _utc_now()
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == "failed",
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.owner_kind == owner_kind,
                    WorkflowRunState.owner_principal_id == owner_principal_id,
                    WorkflowRunState.service_id == service_id,
                )
                .values(
                    status="queued",
                    failure_reason=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    effect_receipts_json=_canonical(_bounded_effect_ledger(existing_effects)),
                    finished_at=None,
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("retry actor is unauthorized or job changed")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "retry",
                "status": "recorded",
                "owner_kind": owner_kind,
                "owner_principal_id": owner_principal_id,
                "service_id": service_id,
                "reconciliation_receipt_digest": receipt_digest,
                "revision": _revision(refreshed),
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

    async def reconcile_external_effect(
        self,
        job_id: str,
        *,
        owner_kind: str,
        owner_principal_id: str,
        service_id: str | None = None,
        reconciliation_receipt: Any,
        target_status: str = "failed",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Resolve an uncertain restart state before any retry is admitted."""
        if target_status not in {"failed", "blocked", "cancelled"}:
            raise DurableJobTransitionError("reconciliation target must be failed, blocked, or cancelled")
        canonical_receipt, receipt_digest = _canonical_reconciliation_receipt(reconciliation_receipt)
        receipt_payload = _json_load(canonical_receipt, {})
        receipt_effect_id = _text(receipt_payload.get("effect_id"))
        receipt_effect_type = _text(receipt_payload.get("effect_type"))
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            effects = _effect_ledger_or_raise(run.effect_receipts_json)
            can_reconcile_failed = run.status == "failed" and _job_has_unsafe_effects(effects)
            can_reconcile_blocked = run.status == "blocked" and _job_has_unsafe_effects(effects)
            if (
                run.status not in UNCERTAIN_EXTERNAL_EFFECT_STATUSES
                and not can_reconcile_failed
                and not can_reconcile_blocked
            ):
                raise DurableJobTransitionError(
                    f"only uncertain jobs or jobs with unresolved effects may be reconciled (current={run.status})"
                )
            _validate_retry_actor(
                run,
                owner_kind=owner_kind,
                owner_principal_id=owner_principal_id,
                service_id=service_id,
            )
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            resolved_effects: list[Any] = []
            matched_effect = False
            for item in effects:
                if (
                    isinstance(item, dict)
                    and _text(item.get("effect_id")) == receipt_effect_id
                    and _text(item.get("effect_type")) == receipt_effect_type
                    and _text(item.get("status")) in {"unknown", "intent", "dispatched"}
                ):
                    receipt_status = _text(receipt_payload.get("status"))
                    _reconciliation_matches_effect(item, receipt_payload)
                    details = item.get("details") if isinstance(item.get("details"), dict) else {}
                    nested_receipt = details.get("receipt") if isinstance(details, dict) else None
                    cost_outstanding = bool(
                        details.get("unknown_cost_outstanding")
                        or (
                            isinstance(nested_receipt, dict)
                            and nested_receipt.get("unknown_cost_outstanding")
                        )
                    )
                    if cost_outstanding and receipt_status != "settled":
                        raise DurableJobTransitionError(
                            "cost liability requires a settled receipt with actual cost and operation binding"
                        )
                    matched_effect = True
                    item = {
                        **item,
                        "reconciled": True,
                        "reconciliation_status": "resolved",
                        "reconciliation_receipt_digest": receipt_digest,
                    }
                resolved_effects.append(item)
            if not matched_effect:
                raise DurableJobTransitionError(
                    "reconciliation_receipt must identify one unresolved effect in the durable ledger"
                )
            resolved_effects.append(
                {
                    "kind": "reconciliation",
                    "status": "reconciled",
                    "receipt": _json_load(canonical_receipt, {}),
                    "receipt_digest": receipt_digest,
                    "owner_kind": owner_kind,
                    "owner_principal_id": owner_principal_id,
                    "service_id": service_id,
                    "recorded_at": _utc_now().isoformat(),
                }
            )
            now = _utc_now()
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == run.status,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.owner_kind == owner_kind,
                    WorkflowRunState.owner_principal_id == owner_principal_id,
                    WorkflowRunState.service_id == service_id,
                )
                .values(
                    status=target_status,
                    failure_reason=("external_effect_reconciled" if target_status == "failed" else target_status),
                    effect_receipts_json=_canonical(_bounded_effect_ledger(resolved_effects)),
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                    heartbeat_at=now,
                    finished_at=(
                        now
                        if target_status in DURABLE_JOB_TERMINAL_STATUSES or target_status == "failed"
                        else None
                    ),
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("job changed during external-effect reconciliation")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "external_effect_reconciliation",
                "status": "recorded",
                "from": run.status,
                "to": target_status,
                "reconciliation_receipt_digest": receipt_digest,
                "revision": _revision(refreshed),
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

    # Alias used by recovery adapters that call the operation by its shorter name.
    reconcile_job = reconcile_external_effect

    async def recover_stale_jobs(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        observed_at = now or _utc_now()
        recovered: list[dict[str, Any]] = []
        async with self._session() as db:
            result = await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.status == "running",
                    or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= observed_at),
                )
            )
            runs = result.scalars().all()
            for run in runs:
                old_owner = run.lease_owner
                expected_token = run.fencing_token
                expected_revision = _revision(run)
                recovered_status, recovery_reason = _restart_recovery_state(run)
                updated = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(
                        WorkflowRunState.run_identity == run.run_identity,
                        WorkflowRunState.status == "running",
                        WorkflowRunState.revision == expected_revision,
                        WorkflowRunState.fencing_token == expected_token,
                    )
                    .values(
                        status=recovered_status,
                        failure_reason=recovery_reason,
                        lease_owner=None,
                        lease_expires_at=None,
                        fencing_token=WorkflowRunState.fencing_token + 1,
                        revision=WorkflowRunState.revision + 1,
                        updated_at=observed_at,
                        heartbeat_at=observed_at,
                    )
                )
                if not _rowcount_is_one(updated):
                    continue
                refreshed = await self._fetch(db, run.run_identity)
                receipt = {
                    "kind": "restart_recovery",
                    "status": "blocked",
                    "reason": recovery_reason,
                    "recovery_state": recovered_status,
                    "previous_owner": old_owner,
                    "fencing_token": refreshed.fencing_token,
                    "revision": _revision(refreshed),
                    "operator_action": (
                        "reconcile_external_effect_and_cost_then_retry_or_cancel"
                        if recovered_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES
                        else "reconcile_effects_then_retry_or_cancel"
                    ),
                    "operator_visible": True,
                }
                db.expunge(refreshed)
                recovered.append(_serialize(refreshed, receipt=receipt))
        return recovered

    def _assert_lease(self, run: WorkflowRunState, *, owner: str | None, fencing_token: int | None) -> None:
        if owner is None and fencing_token is None:
            return
        if not owner or fencing_token is None or run.lease_owner != owner or run.fencing_token != fencing_token:
            raise DurableJobLeaseError("active owner lease and fencing token are required")
        try:
            persisted_expiry = _as_utc(run.lease_expires_at)
        except ValueError as exc:
            raise DurableJobLeaseError("job lease metadata is malformed") from exc
        if persisted_expiry is None or persisted_expiry <= _utc_now():
            raise DurableJobLeaseError("job lease has expired")

    async def _fetch(self, db: Any, job_id: str) -> WorkflowRunState:
        # Bulk CAS updates deliberately disable ORM session synchronization so
        # timezone-aware predicates are evaluated by the database. Refresh the
        # identity-map row on every read before serializing the receipt.
        run = (
            await db.execute(
                select(WorkflowRunState)
                .where(WorkflowRunState.run_identity == job_id)
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
        if run is None:
            raise DurableJobNotFound(job_id)
        return run

    @staticmethod
    def _session():
        # Resolve dynamically so the existing durable_state DB fixture and
        # migration shims can patch one canonical session factory.
        from src.workflows import durable_state

        return durable_state.get_session()


durable_job_repository = DurableJobRepository()


__all__ = [
    "DURABLE_JOB_RECORD_SCHEMA_VERSION",
    "DURABLE_JOB_STATUSES",
    "DURABLE_JOB_TRANSITIONS",
    "DURABLE_JOB_TERMINAL_STATUSES",
    "DEPENDENCY_FAILURE_STATUSES",
    "DEPENDENCY_UNRESOLVED_STATUSES",
    "RECONCILIATION_RECEIPT_STATUSES",
    "UNCERTAIN_EXTERNAL_EFFECT_STATUSES",
    "UNRESOLVED_EFFECT_STATUSES",
    "REMOTE_INFERENCE_RECEIPT_STATUSES",
    "REMOTE_INFERENCE_EFFECT_STATUSES",
    "DurableJobError",
    "DurableJobNotFound",
    "DurableJobTransitionError",
    "DurableJobIdempotencyConflict",
    "DurableJobLeaseError",
    "DurableJobIdentity",
    "DurableJobSpec",
    "DurableJobRepository",
    "_canonical_remote_inference_receipt",
    "durable_job_repository",
]
