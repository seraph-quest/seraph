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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import false, func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import select

from src.artifacts.registry import build_artifact_record
from src.db.models import ApprovalRequest, Goal, GuardianRoutine, GuardianRoutineVersion, WorkflowRunState
from src.db.session_refs import ensure_sessions_exist
from src.workflows.inference_accounting import InferenceAccountingRepositoryMixin


@dataclass(frozen=True)
class NodeProcessCleanupSettlement:
    """Internal original authority request, never a caller-supplied proof."""
    job_id: str
    expected_revision: int
    owner_principal_id: str
    owner_session_id: str
    authority: Mapping[str, Any]
    dispatch: Mapping[str, Any]


DURABLE_JOB_RECORD_SCHEMA_VERSION = 2

# A repair execution reservation is a safety boundary rather than ordinary
# checkpoint history.  It must survive bounded history churn until the exact
# same job/attempt/fence publishes cleanup and readback proof.  Keep the
# latest receipt for each of these ids when trimming any checkpoint history.
_REPO_REPAIR_RESERVATION_CHECKPOINT_IDS = frozenset(
    {
        "repo-repair-execution-reservation",
        "repo-repair-execution-release",
    }
)


def get_session():
    """Resolve the shared session factory through the durable-state module.

    Keeping this narrow proxy makes the canonical repository's database
    dependency explicit and patchable in isolated process/database fixtures
    while preserving the runtime migration hook that owns the session factory.
    """
    from src.workflows import durable_state

    return durable_state.get_session()

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
    "degraded",
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
        "degraded",
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
    # A failed local execution can be explicitly settled when recovery has
    # proved that no external effect remains. Unknown effect history is
    # redirected to reconciliation below rather than silently cancelled.
    "failed": frozenset({"queued", "cancelled", "unknown_external_effect", "cost_liability"}),
    "degraded": frozenset(),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
}
DURABLE_JOB_TERMINAL_STATUSES = frozenset({"succeeded", "degraded", "cancelled"})
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


class DurableJobAdmissionDenied(DurableJobError, ValueError):
    """Raised when a persisted bounded admission budget is exhausted."""

    def __init__(self, reason: str, *, goal_id: str | None = None) -> None:
        self.reason = str(reason)
        self.goal_id = goal_id
        super().__init__(self.reason)


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


def durable_lease_id(job_id: str, fencing_token: int) -> str:
    """Derive the stable identity for one persisted lease epoch.

    ``WorkflowRunState`` predates an explicit lease-id column.  The job
    identity and fencing token are both durable and immutable for a lease
    epoch, so their digest gives recovery a stable, non-secret lease handle
    without introducing another mutable source of truth.
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        raise ValueError("job_id is required to derive a lease id")
    try:
        normalized_fencing_token = int(fencing_token)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("fencing_token is required to derive a lease id") from exc
    if normalized_fencing_token < 0:
        raise ValueError("fencing_token must be nonnegative")
    digest = _digest(
        {
            "job_id": normalized_job_id,
            "fencing_token": normalized_fencing_token,
        }
    )
    return f"lease:{digest[:32]}"


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


def _bounded_checkpoint_receipts(
    history: Iterable[Any],
    *,
    limit: int = 50,
) -> list[Any]:
    """Trim checkpoint history while retaining an unresolved repair hold.

    Checkpoint receipts are intentionally bounded because they are persisted
    in one JSON column.  A repair reservation is also the cross-process
    execution lock, so evicting its held receipt would let a successor pass
    the durable admission query after a worker crash.  Preserve the latest
    reservation and release receipts within the same bound and use the
    remaining slots for ordinary history.  Malformed special receipts are
    retained too; the repair parser then fails closed instead of treating
    corruption as an absent reservation.
    """

    try:
        bounded_limit = max(1, int(limit))
    except (TypeError, ValueError, OverflowError):
        bounded_limit = 50
    items = list(history) if isinstance(history, Iterable) else []
    latest_special: dict[str, tuple[int, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        checkpoint_id = _text(item.get("checkpoint_id"))
        if checkpoint_id in _REPO_REPAIR_RESERVATION_CHECKPOINT_IDS:
            latest_special[checkpoint_id] = (index, item)
    special_indexes = {index for index, _item in latest_special.values()}
    ordinary = [
        (index, item)
        for index, item in enumerate(items)
        if index not in special_indexes
    ]
    retained_special = list(latest_special.values())
    ordinary_slots = max(0, bounded_limit - len(retained_special))
    selected = ordinary[-ordinary_slots:] if ordinary_slots else []
    selected.extend(retained_special)
    selected.sort(key=lambda pair: pair[0])
    return [item for _index, item in selected]


def _cleanup_reservation_pending(run: WorkflowRunState) -> bool:
    """Return whether an exact private cleanup is between intent and receipt."""

    checkpoints = _json_load(getattr(run, "checkpoint_receipts_json", None), [])
    if not isinstance(checkpoints, list):
        return False
    for item in reversed(checkpoints):
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("kind") == "repo_repair_artifact_cleanup":
            return payload.get("status") in {"deletion_pending", "cleanup_required"}
    return False


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


def _resolve_readback_observations(
    effects: list[dict[str, Any]], receipt: dict[str, Any]
) -> list[dict[str, Any]]:
    """Resolve diagnostics of this exact verified effect without erasing them.

    Called within the successful readback's existing revision CAS. A diagnostic
    never settles its parent, another operation, or an independent liability.
    """
    parent_id = receipt.get("effect_id")
    if (
        not isinstance(parent_id, str)
        or sum(item.get("effect_id") == parent_id for item in effects) != 1
        or receipt.get("reconciled") is not True
        or receipt.get("reconciliation_status") != "resolved"
        or not _verified_readback_exists([receipt])
        or not _text(receipt.get("readback_id"))
        or not _text(receipt.get("target_digest"))
    ):
        return effects
    try:
        verified_at = _as_utc(receipt.get("verified_at"))
    except (TypeError, ValueError):
        return effects
    if verified_at is None:
        return effects
    resolved = []
    for item in effects:
        details = item.get("details")
        nested = details.get("receipt") if isinstance(details, dict) else None
        try:
            observed_at = _as_utc(item.get("recorded_at"))
        except (TypeError, ValueError):
            observed_at = None
        matches = (
            item.get("receipt_kind") == "readback"
            and item.get("original_effect_id") == parent_id
            and isinstance(details, dict)
            and details.get("readback_observation_only") is True
            and details.get("verified") is False
            and not details.get("reconciliation_required")
            and not details.get("unknown_cost_outstanding")
            and not (isinstance(nested, dict) and (nested.get("reconciliation_required") or nested.get("unknown_cost_outstanding")))
            and item.get("effect_id") == f"{parent_id}:readback:{_digest({'status': item.get('status'), 'target_path': item.get('target_path')})[:16]}"
            and all(item.get(field) == receipt.get(field) for field in ("effect_type", "target_path", "target_digest", "approval_id", "adapter_idempotency_key"))
            and observed_at is not None and observed_at <= verified_at
        )
        resolved.append({**item, "reconciled": True, "reconciliation_status": "resolved", "resolution_parent_effect_id": parent_id, "resolution_readback_id": receipt["readback_id"], "resolution_verified_at": receipt["verified_at"]} if matches else item)
    return resolved


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


def _terminal_remote_settlement_effect(
    effects: Iterable[Any],
    *,
    expected_job_id: str | None = None,
    expected_owner_id: str | None = None,
) -> Mapping[str, Any] | None:
    """Find a broker-settled remote effect that can close a crashed run.

    The broker receipt is the durable readback for a remote invocation.  It
    contains the operation/job/owner binding and a terminal success status;
    unlike an ``intent`` or ``blocked`` projection it carries no unresolved
    external liability.  Recovery uses this narrow shape so an arbitrary
    successful effect can never promote a stale job to ``succeeded``.
    """
    for item in effects:
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("effect_type")) != "remote_inference_admission":
            continue
        if _text(item.get("status")) != "succeeded":
            continue
        details = item.get("details")
        if not isinstance(details, Mapping):
            continue
        nested = details.get("receipt")
        if not isinstance(nested, Mapping):
            continue
        if _text(details.get("admission_status")) not in {"succeeded", "settled"}:
            continue
        if _text(nested.get("status")) not in {"succeeded", "settled"}:
            continue
        operation_id = _text(nested.get("operation_id"))
        job_id = _text(nested.get("job_id"))
        owner_id = _text(nested.get("owner_id"))
        if not operation_id or not job_id or not owner_id:
            continue
        if expected_job_id is not None and job_id != expected_job_id:
            continue
        if expected_owner_id is not None and owner_id != expected_owner_id:
            continue
        if _text(item.get("target_digest")) != operation_id:
            continue
        if details.get("reconciliation_required") or details.get("unknown_cost_outstanding"):
            continue
        if nested.get("reconciliation_required") or nested.get("unknown_cost_outstanding"):
            continue
        return item
    return None


def _remote_terminal_recovery_readback(
    effect: Mapping[str, Any],
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    """Build a verified readback marker for terminal remote settlement."""
    details = effect.get("details")
    nested = details.get("receipt") if isinstance(details, Mapping) else None
    nested = nested if isinstance(nested, Mapping) else {}
    effect_id = _text(effect.get("effect_id"))
    settlement_digest = _text(details.get("receipt_digest")) if isinstance(details, Mapping) else ""
    settlement_digest = settlement_digest or _digest(dict(nested))
    return {
        "kind": "remote_inference_terminal_recovery",
        "receipt_kind": "readback",
        "effect_id": f"{effect_id}:terminal_readback",
        "original_effect_id": effect_id,
        "effect_type": "remote_inference_admission",
        "target_path": _text(effect.get("target_path")),
        "target_digest": _text(effect.get("target_digest")),
        "status": "succeeded",
        "content_sha256": settlement_digest,
        "details": {
            "verified": True,
            "source": "durable_remote_settlement",
            "operation_id": _text(nested.get("operation_id")),
            "settlement_receipt_digest": settlement_digest,
        },
        "recorded_at": observed_at.isoformat(),
        "fencing_token": effect.get("fencing_token"),
        "reconciled": True,
        "reconciliation_status": "resolved",
    }


def _safe_structure(value: Any, *, max_depth: int = 3) -> Any:
    """Keep durable receipts structural and never persist secret values."""
    if max_depth <= 0:
        return "[redacted]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            # Parent and board fencing counters are monotonic CAS identities,
            # not bearer credentials. Preserve only their typed non-negative
            # integer form so durable child authority can be rebound and
            # verified after recovery; all other token-like values remain
            # redacted, including strings and negative values.
            is_fencing_counter = (
                normalized_key in {
                    "parentfencingtoken",
                    "boardfencingtoken",
                    "routineparentfencingtoken",
                    "routineparentboardfencingtoken",
                    "watchparentfencingtoken",
                    "watchparentboardfencingtoken",
                    # The repair lane stores its exact durable reservation
                    # identity under the short ``fence`` field.  It is a
                    # monotonic CAS counter, so retaining its non-negative
                    # integer form is safe and required for same-job
                    # recovery/settlement.
                    "fence",
                }
                and type(item) is int
                and item >= 0
            )
            if not is_fencing_counter and (any(
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
            ) or normalized_key in {"originalerror", "errordetail", "traceback"}):
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


_ROUTINE_PUBLICATION_BINDING_FIELDS = frozenset(
    {
        "routine_id",
        "routine_revision",
        "routine_version",
        "package_digest",
        "parent_invocation_job_id",
        "publication_child_job_id",
        "invocation_uuid",
        "owner_principal_id",
        "owner_session_id",
        "goal_id",
        "goal_revision",
        "source_watch_id",
        "connection_id",
        "connection_revision",
        "repository",
        "action",
        "operation_uuid",
    }
)
_ROUTINE_PUBLICATION_BINDING_REVISIONS = frozenset(
    {"routine_revision", "routine_version", "goal_revision", "connection_revision"}
)


def _safe_routine_publication_binding(value: Any) -> dict[str, Any] | None:
    """Persist only the scalar server binding needed to fence routine M3."""
    if not isinstance(value, Mapping) or set(value) != _ROUTINE_PUBLICATION_BINDING_FIELDS:
        return None
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if key in _ROUTINE_PUBLICATION_BINDING_REVISIONS:
            if type(item) is not int or item <= 0:
                return None
            safe[key] = item
        elif not isinstance(item, str) or not item or len(item) > 512:
            return None
        else:
            safe[key] = item
    return safe


def _safe_durable_authority(
    value: Any, *, repo_node_posture_expectation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe = _safe_structure(value)
    if not isinstance(safe, dict) or not isinstance(value, Mapping):
        return safe if isinstance(safe, dict) else {}
    binding = _safe_routine_publication_binding(value.get("routine_binding"))
    if binding is not None:
        safe["routine_binding"] = binding
    if value.get("sandbox_profile") == "repo-node24-npm-v1":
        from src.execution.repo_node import safe_node_posture

        posture = safe_node_posture(value, expected_posture=repo_node_posture_expectation)
        if posture is None:
            raise DurableJobIdempotencyConflict("Node posture requires independent selected-executor preflight facts")
        safe["executor_posture"] = posture
    return safe


def _safe_durable_inputs(inputs: Any) -> tuple[str, dict[str, Any]]:
    digest, projection = _safe_inputs_digest(inputs)
    binding = (
        _safe_routine_publication_binding(inputs.get("routine_binding"))
        if isinstance(inputs, Mapping)
        else None
    )
    if binding is not None:
        projection["routine_binding"] = binding
    return digest, projection


def _positive_revision(value: Any) -> int | None:
    """Accept only positive JSON integer revisions at durable boundaries."""
    if type(value) is not int or value <= 0:
        return None
    return value


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
    if not _text(spec.goal_id) and spec.goal_revision is not None:
        raise ValueError("goal_revision requires a canonical goal")
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
    authority_session_id = authority.get("session_id")
    if authority_session_id is not None and _text(authority_session_id) != _text(spec.session_id):
        raise ValueError("declared authority session_id must match session_id")
    if identity.owner_kind == "service" and _text(authority_service_id) != _text(spec.service_id):
        raise ValueError("service authority must declare the matching service_id")
    if identity.owner_kind == "user" and _text(authority_service_id):
        raise ValueError("user authority cannot declare service_id")
    for field_name in ("goal_revision", "plan_revision"):
        value = getattr(spec, field_name, None)
        if value is not None and _positive_revision(value) is None:
            raise ValueError(f"{field_name} must be a positive JSON integer")


def _goal_revision(value: Any, *, field_name: str = "goal_revision") -> int:
    """Return a strict positive goal revision for a durable fence."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DurableJobTransitionError(f"{field_name} must be a positive integer")
    return value


def _goal_owner_binding(goal: Goal) -> tuple[str, str]:
    """Return the complete canonical owner/session pair or fail closed."""
    owner = _text(getattr(goal, "owner_principal_id", None))
    session = _text(getattr(goal, "owner_session_id", None))
    if not owner or not session:
        raise DurableJobTransitionError("canonical goal owner/session binding is missing")
    return owner, session


def _goal_authority_binding(value: Any) -> tuple[str, str]:
    """Read the delegated target owner binding from a service authority."""
    authority = value if isinstance(value, Mapping) else _json_load(value, {})
    if not isinstance(authority, Mapping):
        authority = {}
    owner = _text(authority.get("goal_owner_principal_id"))
    session = _text(authority.get("goal_owner_session_id"))
    delegated = authority.get("goal_owner")
    if isinstance(delegated, Mapping):
        owner = owner or _text(delegated.get("principal_id") or delegated.get("owner_principal_id"))
        session = session or _text(delegated.get("session_id") or delegated.get("owner_session_id"))
    if not owner or not session:
        raise DurableJobTransitionError("service goal authority owner/session binding is missing")
    return owner, session


def _canonical_goal_max_outstanding(goal: Goal | None) -> int | None:
    """Read the already persisted Goal admission cap without caller input.

    Native procedure children use the same effective Goal cap as their parent
    Browser/Calendar adapter.  A missing legacy budget retains the historical
    serial child contract; a present malformed budget fails closed.
    """

    raw = getattr(goal, "admission_budget_json", None) if goal is not None else None
    if not raw:
        return None
    try:
        from src.goals.contracts import GoalAdmissionBudget

        return int(GoalAdmissionBudget.model_validate(json.loads(raw)).max_outstanding_jobs)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableJobTransitionError("canonical goal admission budget is malformed") from exc


def _is_typed_admission_receipt(
    run: WorkflowRunState,
    *,
    effect_type: str,
    receipt_kind: str,
    status: str,
    details: Any,
    owner: str | None,
    fencing_token: int | None,
) -> bool:
    """Allow only the narrow ownerless receipt used before a claim.

    Admission authority denials are the one legitimate write before a durable
    job has a lease. Keep that projection typed and harmless: an arbitrary
    caller must not be able to append a success, intent, or external-effect
    receipt to an accepted row without a lease fence.
    """
    return bool(
        _text(getattr(run, "status", None)) == "accepted"
        and owner is None
        and fencing_token is None
        and effect_type == "authority_gate"
        and receipt_kind == "effect"
        and status == "blocked"
        and isinstance(details, Mapping)
        and _text(details.get("decision")) == "deny"
        and isinstance(details.get("redacted_receipt"), Mapping)
    )


async def _assert_canonical_goal_fence(
    db: Any,
    *,
    goal_id: str | None,
    goal_revision: int | None,
    owner_kind: str,
    owner_principal_id: str | None,
    session_id: str | None,
    authority: Any = None,
) -> Goal | None:
    """Check the canonical goal before a durable job can execute or mutate.

    User jobs are directly bound to the goal owner/session. Service jobs use an
    explicit delegated authority binding because their worker session is not
    the user's operator session. The SQL predicate added by
    ``_append_goal_fence_condition`` repeats existence, active status, and
    revision at the final CAS boundary, closing the read/check/write race.
    """
    if goal_id is None or not _text(goal_id):
        if goal_revision is not None:
            raise DurableJobTransitionError("goal_revision requires a canonical goal")
        return None
    revision = _goal_revision(goal_revision)
    result = await db.execute(select(Goal).where(Goal.id == str(goal_id)))
    goal = result.scalars().first()
    if goal is None:
        raise DurableJobTransitionError("canonical goal does not exist")
    canonical_revision = _goal_revision(
        max(int(getattr(goal, "revision", 1) or 1), 1),
        field_name="canonical goal revision",
    )
    if canonical_revision != revision:
        raise DurableJobTransitionError("durable job goal revision is stale")
    goal_status = getattr(getattr(goal, "status", None), "value", getattr(goal, "status", None))
    if _text(goal_status) != "active":
        raise DurableJobTransitionError("canonical goal is not active")
    canonical_owner, canonical_session = _goal_owner_binding(goal)
    if owner_kind == "user":
        if _text(owner_principal_id) != canonical_owner:
            raise DurableJobTransitionError("durable job goal owner is stale")
        if _text(session_id) != canonical_session:
            raise DurableJobTransitionError("durable job goal session is stale")
    elif owner_kind == "service":
        authority_value = authority if isinstance(authority, Mapping) else _json_load(authority, {})
        declared_session_id = (
            _text(authority_value.get("session_id"))
            if isinstance(authority_value, Mapping)
            else ""
        )
        if declared_session_id and declared_session_id != _text(session_id):
            raise DurableJobTransitionError("service goal authority session is stale")
        delegated_owner, delegated_session = _goal_authority_binding(authority)
        if (delegated_owner, delegated_session) != (canonical_owner, canonical_session):
            raise DurableJobTransitionError("service goal authority owner/session is stale")
    else:
        raise DurableJobTransitionError("durable job owner kind is invalid")
    return goal


def _append_goal_fence_condition(conditions: list[Any], run: WorkflowRunState) -> None:
    """Repeat the canonical goal fence in the final durable row CAS."""
    goal_id = _text(getattr(run, "goal_id", None))
    if not goal_id:
        if getattr(run, "goal_revision", None) is not None:
            conditions.append(false())
        return
    try:
        revision = _goal_revision(getattr(run, "goal_revision", None))
    except DurableJobTransitionError:
        conditions.append(false())
        return
    owner_kind = _text(getattr(run, "owner_kind", None))
    if owner_kind == "user":
        expected_owner = _text(getattr(run, "owner_principal_id", None))
        expected_session = _text(getattr(run, "session_id", None))
    elif owner_kind == "service":
        try:
            expected_owner, expected_session = _goal_authority_binding(
                getattr(run, "declared_authority_json", None)
            )
        except DurableJobTransitionError:
            conditions.append(false())
            return
    else:
        conditions.append(false())
        return
    if not expected_owner or not expected_session:
        conditions.append(false())
        return
    goal = aliased(Goal)
    conditions.append(
        select(goal.id)
        .where(
            goal.id == goal_id,
            goal.revision == revision,
            goal.status == "active",
            goal.owner_principal_id == expected_owner,
            goal.owner_session_id == expected_session,
        )
        .exists()
    )


def _append_stale_goal_revision_condition(
    conditions: list[Any],
    run: WorkflowRunState,
    *,
    observed_at: datetime,
) -> None:
    """Fence one stale-revision cleanup to the exact expired durable row.

    The normal goal predicate cannot match after the canonical goal advances.
    Recovery still needs an atomic cleanup path, however, or an expired worker
    lease can permanently occupy the outstanding-job budget. Keep the old
    persisted owner/authority/goal identity in the CAS and require the current
    goal to be a later revision with the same canonical owner/session. This
    path only clears the lease and blocks the run; it never writes an effect.
    """
    goal_id = _text(getattr(run, "goal_id", None))
    if not goal_id:
        conditions.append(false())
        return
    try:
        revision = _goal_revision(getattr(run, "goal_revision", None))
    except DurableJobTransitionError:
        conditions.append(false())
        return

    owner_kind = _text(getattr(run, "owner_kind", None))
    if owner_kind == "user":
        expected_owner = _text(getattr(run, "owner_principal_id", None))
        expected_session = _text(getattr(run, "session_id", None))
    elif owner_kind == "service":
        try:
            expected_owner, expected_session = _goal_authority_binding(
                getattr(run, "declared_authority_json", None)
            )
        except DurableJobTransitionError:
            conditions.append(false())
            return
    else:
        conditions.append(false())
        return
    if not expected_owner or not expected_session:
        conditions.append(false())
        return

    def _match(column: Any, value: Any) -> None:
        conditions.append(column == value if value is not None else column.is_(None))

    # Preserve every immutable identity field that could otherwise allow a
    # stale recovery pass to settle a different row after a concurrent write.
    for column, value in (
        (WorkflowRunState.run_identity, getattr(run, "run_identity", None)),
        (WorkflowRunState.owner_kind, getattr(run, "owner_kind", None)),
        (WorkflowRunState.owner_principal_id, getattr(run, "owner_principal_id", None)),
        (WorkflowRunState.service_id, getattr(run, "service_id", None)),
        (WorkflowRunState.session_id, getattr(run, "session_id", None)),
        (WorkflowRunState.goal_id, getattr(run, "goal_id", None)),
        (WorkflowRunState.goal_revision, getattr(run, "goal_revision", None)),
        (WorkflowRunState.parent_job_id, getattr(run, "parent_job_id", None)),
        (WorkflowRunState.parent_fencing_token, getattr(run, "parent_fencing_token", None)),
        (WorkflowRunState.authority_digest, getattr(run, "authority_digest", None)),
        (WorkflowRunState.declared_authority_json, getattr(run, "declared_authority_json", None)),
    ):
        _match(column, value)
    _match(WorkflowRunState.lease_owner, getattr(run, "lease_owner", None))
    conditions.append(
        or_(
            WorkflowRunState.lease_expires_at.is_(None),
            WorkflowRunState.lease_expires_at <= observed_at,
        )
    )

    goal = aliased(Goal)
    conditions.append(
        select(goal.id)
        .where(
            goal.id == goal_id,
            goal.revision > revision,
            goal.owner_principal_id == expected_owner,
            goal.owner_session_id == expected_session,
        )
        .exists()
    )


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
    """Require canonical goal identity and, for children, the live parent fence."""
    _append_goal_fence_condition(conditions, run)
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
    if spec.run_fingerprint is not None or hasattr(existing, "run_fingerprint"):
        expected["run_fingerprint"] = _text(spec.run_fingerprint, input_digest)
    if hasattr(existing, "budget_digest"):
        expected_budget = spec.budget_microusd
        if expected_budget is None:
            expected_budget = _authority_budget_microusd(spec.declared_authority)
        expected["budget_digest"] = _text(
            spec.budget_digest,
            _digest({"budget_microusd": expected_budget}),
        )
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
    if "run_fingerprint" in expected:
        actual["run_fingerprint"] = getattr(existing, "run_fingerprint", None) or input_digest
    if "budget_digest" in expected:
        authority = _json_load(getattr(existing, "declared_authority_json", None), {})
        actual["budget_digest"] = getattr(existing, "budget_digest", None) or _digest(
            {"budget_microusd": _authority_budget_microusd(authority)}
        )
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
    effect: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    require_unresolved: bool = True,
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
        if not observed_digest or not expected_digest:
            raise DurableJobTransitionError(
                "readback reconciliation requires the exact intended target digest"
            )
        if observed_digest != expected_digest:
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
    if require_unresolved and not _effect_is_unresolved(effect):
        raise DurableJobTransitionError(
            "reconciliation_receipt must identify an unresolved durable effect"
        )


def _retry_reconciliation_marker_matches(
    effects: Iterable[Any],
    effect: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_digest: str,
) -> bool:
    """Allow retry only for the exact receipt that already settled this effect.

    ``reconcile_external_effect`` resolves an unresolved ledger item before a
    retry is admitted.  The original item is consequently marked resolved, so
    retry must use the durable reconciliation marker produced by that call;
    a normal succeeded/readback item or an unrelated receipt is never enough.
    """
    if not effect.get("reconciled") and _text(effect.get("reconciliation_status")) not in {
        "reconciled",
        "resolved",
    }:
        return False
    for item in effects:
        if not isinstance(item, Mapping) or _text(item.get("kind")) != "reconciliation":
            continue
        if _text(item.get("receipt_digest")) != receipt_digest:
            continue
        marker_receipt = item.get("receipt")
        if not isinstance(marker_receipt, Mapping):
            continue
        if (
            _text(marker_receipt.get("effect_id")) != _text(effect.get("effect_id"))
            or _text(marker_receipt.get("effect_type")) != _text(effect.get("effect_type"))
        ):
            continue
        try:
            _reconciliation_matches_effect(
                effect,
                receipt,
                require_unresolved=False,
            )
        except DurableJobError:
            return False
        return True
    return False


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
    request_idempotency_key = _bounded_identifier(
        raw.get("request_idempotency_key"),
        field_name="request_idempotency_key",
        limit=160,
    ) if raw.get("request_idempotency_key") is not None else None
    authority = _json_load(getattr(run, "declared_authority_json", None), {})
    expected_approval_id = _authority_approval_id(authority)
    if not approval_id or not expected_approval_id or approval_id != expected_approval_id:
        raise DurableJobTransitionError("approval resume binding does not match the durable authority")
    if _text(raw.get("authority_digest")) != _text(getattr(run, "authority_digest", None)):
        raise DurableJobTransitionError("approval resume authority has changed")
    required_fields = ["capability_version", "budget_microusd"]
    if getattr(run, "goal_id", None) is not None:
        required_fields.extend(("goal_id", "goal_revision", "plan_revision"))
    missing = [field_name for field_name in required_fields if field_name not in raw]
    if missing:
        raise DurableJobTransitionError(
            "approval resume is missing current " + ", ".join(missing)
        )
    for field_name in ("goal_id", "goal_revision", "plan_revision", "capability_version"):
        expected = getattr(run, field_name, None)
        actual = raw.get(field_name)
        if field_name in {"goal_id", "goal_revision", "plan_revision"} and expected is None:
            if actual not in (None, ""):
                raise DurableJobTransitionError(f"approval resume {field_name} is stale")
            continue
        if field_name in {"goal_revision", "plan_revision"}:
            expected_revision = _positive_revision(expected)
            actual_revision = _positive_revision(actual)
            if expected_revision is None or actual_revision is None:
                raise DurableJobTransitionError(
                    f"approval resume {field_name} is malformed"
                )
            actual = actual_revision
            expected = expected_revision
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
        "request_idempotency_key": request_idempotency_key,
        "recorded_at": now.isoformat(),
    }


def _serialize(run: WorkflowRunState, *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an operator-safe durable job projection."""
    payload = {
        "job_id": run.run_identity,
        "run_identity": run.run_identity,
        "record_schema_version": int(getattr(run, "record_schema_version", DURABLE_JOB_RECORD_SCHEMA_VERSION) or 0),
        "root_run_identity": getattr(run, "root_run_identity", None),
        "parent_run_identity": getattr(run, "parent_run_identity", None),
        "parent_job_id": getattr(run, "parent_job_id", None),
        "parent_fencing_token": getattr(run, "parent_fencing_token", None),
        "branch_kind": getattr(run, "branch_kind", None),
        "branch_depth": int(getattr(run, "branch_depth", 0) or 0),
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
        "conversation_id": getattr(run, "conversation_id", None) or run.session_id,
        "operator_session_id": getattr(run, "operator_session_id", None),
        "run_fingerprint": getattr(run, "run_fingerprint", None),
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
        "budget_digest": getattr(run, "budget_digest", None),
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
            "lease_id": durable_lease_id(
                run.run_identity,
                int(getattr(run, "fencing_token", 0) or 0),
            ),
            "revision": _revision(run),
        },
        "revision": _revision(run),
        "attempt_count": int(getattr(run, "attempt_count", 0) or 0),
        "max_attempts": int(getattr(run, "max_attempts", 1) or 1),
        "failure_reason": getattr(run, "failure_reason", None),
        "result": {
            "digest": getattr(run, "result_digest", None),
            "summary": getattr(run, "result_summary", None),
            # Keep a degraded execution distinguishable from a failed or
            # successful goal in every operator readback projection.
            "status": run.status,
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
class DurableJobRoutinePublicationAdmissionGuard:
    """Internal CAS binding for a routine-owned GitHub publication job.

    The publication (M3) intentionally remains outside the routine
    invocation job tree so an approval can survive a parent fence rollover.
    Its admission nevertheless has to be serialized with cancellation of the
    routine parent and wrapper child.  These fields are the exact durable
    identities and fences that the admission transaction rechecks.
    """

    routine_parent_job_id: str
    routine_parent_fencing_token: int
    publication_child_job_id: str
    publication_child_fencing_token: int
    publication_child_parent_fencing_token: int
    owner_principal_id: str
    owner_session_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "routine_parent_job_id",
            "publication_child_job_id",
            "owner_principal_id",
            "owner_session_id",
        ):
            if not _text(getattr(self, field_name)):
                raise ValueError(f"{field_name} is required")
        for field_name in (
            "routine_parent_fencing_token",
            "publication_child_fencing_token",
            "publication_child_parent_fencing_token",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class DurableJobSpec:
    identity: DurableJobIdentity
    inputs: Any = field(default_factory=dict)
    session_id: str | None = None
    conversation_id: str | None = None
    operator_session_id: str | None = None
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
    max_outstanding_jobs: int | None = None
    service_id: str | None = None
    # ``run_fingerprint`` is the caller's complete immutable execution
    # contract fingerprint.  Older callers omit it and retain the input
    # digest fallback; canonical workflow admission always supplies it.
    run_fingerprint: str | None = None
    budget_microusd: int | None = None
    budget_digest: str | None = None
    # M3 routine publications are admitted outside the durable parent tree so
    # reviewed approvals can be adopted after a parent fence rollover.  When
    # supplied, the repository validates this binding in the same transaction
    # that inserts the M3 row.  Standalone M3 jobs leave it unset.
    routine_publication_admission_guard: DurableJobRoutinePublicationAdmissionGuard | None = None


class DurableJobRepository(InferenceAccountingRepositoryMixin):
    """Persistence operations for the one canonical workflow job record."""

    async def _assert_routine_publication_admission_guard(
        self,
        db: Any,
        *,
        spec: DurableJobSpec,
        guard: DurableJobRoutinePublicationAdmissionGuard,
        now: datetime,
        dialect_name: str,
    ) -> None:
        """Fence an out-of-tree routine publication before its M3 insert.

        Cancellation settles the routine child and parent in durable state,
        while the GitHub publication remains a separate job for approval
        recovery.  The final M3 admission must therefore lock and validate
        both rows in this transaction; a preflight projection alone leaves a
        cancellation/admission window.
        """

        if spec.identity.job_kind != "github_followthrough_v1":
            raise DurableJobTransitionError(
                "routine publication admission guard has an invalid job kind"
            )
        if spec.identity.owner_kind != "user":
            raise DurableJobTransitionError(
                "routine publication admission guard requires a user job"
            )
        if (
            spec.identity.owner_principal_id != guard.owner_principal_id
            or _text(spec.session_id) != guard.owner_session_id
            or _text(spec.operator_session_id or spec.session_id) != guard.owner_session_id
        ):
            raise DurableJobLeaseError("routine publication admission owner binding is stale")

        parent_id = guard.routine_parent_job_id
        child_id = guard.publication_child_job_id
        if parent_id == child_id or parent_id == spec.identity.job_id or child_id == spec.identity.job_id:
            raise DurableJobTransitionError(
                "routine publication admission identities are invalid"
            )

        statement = select(WorkflowRunState).where(
            WorkflowRunState.run_identity.in_((parent_id, child_id))
        )
        if dialect_name != "sqlite":
            # The SQLite path is protected by BEGIN IMMEDIATE in admit_job;
            # row locks provide the equivalent serialization on a server DB.
            statement = statement.with_for_update()
        rows = (await db.execute(statement)).scalars().all()
        by_identity = {_text(row.run_identity): row for row in rows}
        parent = by_identity.get(parent_id)
        child = by_identity.get(child_id)
        if parent is None or child is None:
            raise DurableJobLeaseError(
                "routine publication admission parent or child is missing"
            )

        def _live_lease(run: WorkflowRunState) -> bool:
            try:
                expiry = _as_utc(run.lease_expires_at)
            except ValueError as exc:
                raise DurableJobLeaseError(
                    "routine publication admission lease metadata is malformed"
                ) from exc
            return bool(_text(run.lease_owner)) and expiry is not None and expiry > now

        if (
            parent.job_kind != "routine_invocation"
            or parent.owner_kind != "user"
            or _text(parent.owner_principal_id) != guard.owner_principal_id
            or _text(parent.session_id) != guard.owner_session_id
            or _text(parent.operator_session_id or parent.session_id) != guard.owner_session_id
            or parent.status != "running"
            or not _live_lease(parent)
            or int(parent.fencing_token or 0) != guard.routine_parent_fencing_token
        ):
            raise DurableJobLeaseError(
                "routine publication admission parent fence is stale or expired"
            )

        if (
            child.job_kind != "routine_github_followthrough_child"
            or child.owner_kind != "user"
            or _text(child.owner_principal_id) != guard.owner_principal_id
            or _text(child.session_id) != guard.owner_session_id
            or _text(child.operator_session_id or child.session_id) != guard.owner_session_id
            or _text(child.parent_job_id) != parent_id
            or int(child.parent_fencing_token or 0)
            != guard.publication_child_parent_fencing_token
            or int(child.fencing_token or 0) != guard.publication_child_fencing_token
            or guard.publication_child_parent_fencing_token
            != guard.routine_parent_fencing_token
            or _text(child.goal_id) != _text(parent.goal_id)
            or child.goal_revision != parent.goal_revision
        ):
            raise DurableJobLeaseError(
                "routine publication admission child binding is stale"
            )

        child_status = _text(child.status)
        if child_status == "running":
            if not _live_lease(child) or int(child.fencing_token or 0) <= 0:
                raise DurableJobLeaseError(
                    "routine publication admission child lease is stale or expired"
                )
        elif child_status == "blocked":
            # This is the canonical approval-held representation after M4
            # settles its wrapper child.  A blocked child with any lease is
            # ambiguous and cannot admit a new out-of-tree publication.
            if child.lease_owner is not None or child.lease_expires_at is not None:
                raise DurableJobLeaseError(
                    "routine publication admission child blocked lease is invalid"
                )
        else:
            raise DurableJobTransitionError(
                "routine publication admission child is not current"
            )

        parent_authority = _json_load(parent.declared_authority_json, {})
        child_authority = _json_load(child.declared_authority_json, {})
        m3_authority = spec.declared_authority
        m3_inputs = spec.inputs
        if not all(
            isinstance(value, Mapping)
            for value in (parent_authority, child_authority, m3_authority, m3_inputs)
        ):
            raise DurableJobTransitionError(
                "routine publication admission authority is malformed"
            )

        authority_binding = _safe_routine_publication_binding(
            m3_authority.get("routine_binding")
        )
        input_binding = _safe_routine_publication_binding(
            m3_inputs.get("routine_binding")
        )
        if authority_binding is None or input_binding is None or authority_binding != input_binding:
            raise DurableJobTransitionError(
                "routine publication admission binding is malformed"
            )
        binding = authority_binding
        routine_statement = select(GuardianRoutine).where(
            GuardianRoutine.id == binding["routine_id"],
            GuardianRoutine.owner_principal_id == guard.owner_principal_id,
            GuardianRoutine.owner_session_id == guard.owner_session_id,
        )
        version_statement = select(GuardianRoutineVersion).where(
            GuardianRoutineVersion.routine_id == binding["routine_id"],
            GuardianRoutineVersion.version == binding["routine_version"],
        )
        if dialect_name != "sqlite":
            # The SQLite path is already inside BEGIN IMMEDIATE.  A row lock
            # on both canonical lifecycle records provides the same admission
            # serialization on a server database.
            routine_statement = routine_statement.with_for_update()
            version_statement = version_statement.with_for_update()
        routine = (await db.execute(routine_statement)).scalars().first()
        version = (await db.execute(version_statement)).scalars().first()
        if routine is None or version is None:
            raise DurableJobLeaseError(
                "routine publication admission canonical routine is missing"
            )
        if (
            _text(routine.state) != "active"
            or int(routine.revision or 0) != binding["routine_revision"]
            or int(routine.current_version or 0) != binding["routine_version"]
            or _text(version.installed_package_digest) != binding["package_digest"]
        ):
            raise DurableJobLeaseError(
                "routine publication admission canonical routine is stale or inactive"
            )
        if (
            binding["parent_invocation_job_id"] != parent_id
            or binding["publication_child_job_id"] != child_id
            or binding["owner_principal_id"] != guard.owner_principal_id
            or binding["owner_session_id"] != guard.owner_session_id
            or binding["goal_id"] != _text(parent.goal_id)
            or binding["goal_revision"] != parent.goal_revision
            or _text(spec.goal_id) != _text(parent.goal_id)
            or spec.goal_revision != parent.goal_revision
        ):
            raise DurableJobLeaseError(
                "routine publication admission binding is stale"
            )

        parent_fields = (
            ("routine_id", "routine_id"),
            ("routine_revision", "routine_revision"),
            ("routine_version", "routine_version"),
            ("package_digest", "package_digest"),
            ("invocation_uuid", "invocation_uuid"),
            ("source_watch_id", "source_watch_id"),
            ("connection_id", "github_connection_id"),
            ("connection_revision", "github_connection_revision"),
            ("repository", "github_repository"),
            ("action", "github_action"),
        )
        child_fields = parent_fields
        if any(parent_authority.get(auth_key) != binding[binding_key] for binding_key, auth_key in parent_fields):
            raise DurableJobLeaseError(
                "routine publication admission parent authority is stale"
            )
        if any(child_authority.get(auth_key) != binding[binding_key] for binding_key, auth_key in child_fields):
            raise DurableJobLeaseError(
                "routine publication admission child authority is stale"
            )
        if (
            _text(child_authority.get("parent_job_id")) != parent_id
            or int(child_authority.get("parent_fencing_token") or 0)
            != guard.routine_parent_fencing_token
            or _text(child_authority.get("routine_invocation_job_id")) != parent_id
            or _text(child_authority.get("step_id")) != "github_followthrough"
            or _text(child_authority.get("m3_job_id")) != spec.identity.job_id
            or _text(child_authority.get("publication_operation_uuid"))
            != binding["operation_uuid"]
        ):
            raise DurableJobLeaseError(
                "routine publication admission child authority is stale"
            )

        # The child checkpoint is the durable identity written before the
        # external prepare call.  It prevents a caller from borrowing a
        # different routine child merely by presenting a matching JSON
        # binding.  Both the pre-admission and approval-held forms are valid;
        # cancellation uses a terminal child status and is rejected above.
        checkpoints = _json_load(child.checkpoint_receipts_json, [])
        if not isinstance(checkpoints, list):
            raise DurableJobTransitionError(
                "routine publication admission child checkpoints are malformed"
            )
        checkpoint = next(
            (
                item
                for item in reversed(checkpoints)
                if isinstance(item, Mapping)
                and item.get("checkpoint_id")
                in {"routine-child:adoption_pending", "routine-child:prepared"}
            ),
            None,
        )
        payload = checkpoint.get("payload") if isinstance(checkpoint, Mapping) else None
        if not isinstance(payload, Mapping) or _text(payload.get("m3_job_id")) != spec.identity.job_id:
            raise DurableJobTransitionError(
                "routine publication admission child checkpoint is stale"
            )

    async def admit_job(
        self, spec: DurableJobSpec, *, repo_node_posture_expectation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Internal server-only copy of actual selected Node preflight facts.
        # A separate method argument cannot be supplied by spec/request
        # serialization and is never persisted as durable authority itself.
        identity = spec.identity
        if not spec.declared_authority:
            raise ValueError("declared_authority is required before admission")
        _validate_admission_authority(spec)
        if not 0 <= int(spec.priority) <= 100:
            raise ValueError("priority must be between 0 and 100")
        if int(spec.max_attempts) < 1:
            raise ValueError("max_attempts must be at least 1")
        if spec.max_outstanding_jobs is not None:
            if isinstance(spec.max_outstanding_jobs, bool) or not 1 <= int(spec.max_outstanding_jobs) <= 16:
                raise ValueError("max_outstanding_jobs must be between 1 and 16")
        if identity.job_id in _string_list(spec.dependencies):
            raise DurableJobTransitionError("a durable job cannot depend on itself")
        deadline = _as_utc(spec.deadline_at)
        now = _utc_now()
        input_digest, safe_inputs = _safe_durable_inputs(spec.inputs)
        run_fingerprint = _bounded_identifier(
            spec.run_fingerprint or input_digest,
            field_name="run_fingerprint",
        )
        authority_budget = _authority_budget_microusd(spec.declared_authority)
        if spec.budget_microusd is not None and authority_budget is not None:
            if int(spec.budget_microusd) != authority_budget:
                raise DurableJobIdempotencyConflict(
                    "budget_microusd conflicts with declared authority"
                )
        budget_microusd = (
            spec.budget_microusd
            if spec.budget_microusd is not None
            else authority_budget
        )
        if budget_microusd is not None:
            if isinstance(budget_microusd, bool) or int(budget_microusd) < 0:
                raise ValueError("budget_microusd must be a nonnegative integer")
            budget_microusd = int(budget_microusd)
        budget_digest = _text(
            spec.budget_digest,
            _digest({"budget_microusd": budget_microusd}),
        )
        if budget_digest != _digest({"budget_microusd": budget_microusd}):
            raise DurableJobIdempotencyConflict("budget_digest does not match the durable budget")
        binding = _binding(
            owner_principal_id=identity.owner_principal_id,
            goal_id=spec.goal_id,
            goal_revision=spec.goal_revision,
            idempotency_scope=identity.idempotency_scope,
            dedupe_key=identity.idempotency_key,
        )
        authority_digest = _digest(spec.declared_authority)
        safe_authority = _safe_durable_authority(
            spec.declared_authority,
            repo_node_posture_expectation=repo_node_posture_expectation,
        )
        root_run_identity = identity.job_id
        branch_depth = 0
        native_procedure_leaf = False
        canonical_goal: Goal | None = None
        async with self._session() as db:
            bind = db.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            transaction_started = False
            if not _text(spec.goal_id) and spec.goal_revision is not None:
                raise DurableJobTransitionError("goal_revision requires a canonical goal")
            if _text(spec.goal_id):
                # The local-first runtime uses SQLite.  Start one immediate
                # write transaction before reading the canonical goal so a
                # goal update/delete cannot race admission. A row-locking
                # backend serializes on the canonical goal row instead.
                if dialect_name == "sqlite":
                    await db.execute(text("BEGIN IMMEDIATE"))
                    transaction_started = True
                else:
                    await db.execute(
                        select(Goal.id)
                        .where(Goal.id == spec.goal_id)
                        .with_for_update()
                    )
                canonical_goal = await _assert_canonical_goal_fence(
                    db,
                    goal_id=spec.goal_id,
                    goal_revision=spec.goal_revision,
                    owner_kind=identity.owner_kind,
                    owner_principal_id=identity.owner_principal_id,
                    session_id=spec.session_id,
                    authority=spec.declared_authority,
                )
            if spec.parent_job_id is not None:
                if spec.parent_fencing_token is None:
                    raise DurableJobLeaseError("parent fencing token is required for child admission")
                try:
                    parent_fence = int(spec.parent_fencing_token)
                except (TypeError, ValueError) as exc:
                    raise DurableJobLeaseError("parent job fence is malformed") from exc
                if parent_fence <= 0:
                    raise DurableJobLeaseError("parent job fence is malformed")
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
                # A durable child belongs to the same cancellation/readback
                # tree as the validated parent.  The parent fence above is
                # the authority that makes this lineage trustworthy; derive
                # the root only after that validation succeeds.  Legacy rows
                # may have an empty root, so retain their run identity as the
                # safe fallback instead of creating a second tree.
                root_run_identity = _text(getattr(parent, "root_run_identity", None))
                if not root_run_identity:
                    root_run_identity = _text(parent.run_identity)
                try:
                    parent_branch_depth = int(getattr(parent, "branch_depth", 0) or 0)
                except (TypeError, ValueError) as exc:
                    raise DurableJobTransitionError(
                        "parent branch depth is malformed"
                    ) from exc
                if parent_branch_depth < 0:
                    raise DurableJobTransitionError("parent branch depth is malformed")
                branch_depth = parent_branch_depth + 1
                authority_parent = _text(spec.declared_authority.get("routine_parent_job_id"))
                authority_parent_fence = spec.declared_authority.get("routine_parent_fencing_token")
                authority_step = _text(spec.declared_authority.get("routine_step_id"))
                # Native Procedure v2 leaves are real children in the
                # durable tree. Their goal and parent fence must match the
                # already validated parent; callers cannot evade the root
                # outstanding budget by naming an unrelated parent. Legacy
                # parented workflows without this server-owned marker retain
                # their historical admission contract.
                if authority_parent or authority_parent_fence is not None or authority_step:
                    if (
                        _text(spec.goal_id) != _text(parent.goal_id)
                        or spec.goal_revision != parent.goal_revision
                    ):
                        raise DurableJobLeaseError("native leaf goal binding is stale")
                if authority_parent or authority_parent_fence is not None or authority_step:
                    try:
                        # The server-owned native marker is part of the
                        # admission fence.  Do not coerce a bool or string
                        # into a valid fence and accidentally make an
                        # untrusted child eligible for the root exemption.
                        if type(authority_parent_fence) is not int or authority_parent_fence <= 0:
                            raise ValueError
                    except (TypeError, ValueError) as exc:
                        raise DurableJobLeaseError("native leaf parent context is malformed") from exc
                    if (
                        authority_parent != _text(spec.parent_job_id)
                        or authority_parent_fence != parent_fence
                        or not authority_step
                        or parent.job_kind != "guardian_routine_v2"
                        or parent.capability_version != "guardian-routine.v2"
                    ):
                        raise DurableJobLeaseError("native leaf parent context is stale")
                    expected_native_step = {
                        "browser_public_task": "public_browser_check",
                        "calendar_meeting_prep": "selected_meeting_prep",
                        "guardian_source_watch": "source_watch",
                    }.get(_text(identity.job_kind))
                    parent_authority = _json_load(
                        getattr(parent, "declared_authority_json", None), {}
                    )
                    parent_owner_principal = _text(
                        getattr(parent, "owner_principal_id", None)
                        or parent_authority.get("principal")
                    )
                    parent_owner_session = _text(
                        getattr(parent, "operator_session_id", None)
                        or getattr(parent, "session_id", None)
                        or parent_authority.get("session_id")
                    )
                    parent_goal_owner_principal = _text(
                        parent_authority.get("goal_owner_principal_id")
                        or parent_owner_principal
                    )
                    parent_goal_owner_session = _text(
                        parent_authority.get("goal_owner_session_id")
                        or parent_owner_session
                    )
                    native_procedure_leaf = bool(
                        expected_native_step
                        and parent_owner_principal
                        and parent_owner_session
                        and parent_goal_owner_principal
                        and parent_goal_owner_session
                        and authority_step == expected_native_step
                        and _text(spec.declared_authority.get("routine_parent_goal_id"))
                        == _text(parent.goal_id)
                        and type(spec.declared_authority.get("routine_parent_goal_revision")) is int
                        and int(spec.declared_authority.get("routine_parent_goal_revision"))
                        == int(parent.goal_revision)
                        and _text(spec.declared_authority.get("routine_parent_owner_principal_id"))
                        == parent_owner_principal
                        and _text(spec.declared_authority.get("routine_parent_owner_session_id"))
                        == parent_owner_session
                        and _text(spec.declared_authority.get("goal_owner_principal_id"))
                        == parent_goal_owner_principal
                        and _text(spec.declared_authority.get("goal_owner_session_id"))
                        == parent_goal_owner_session
                        and _text(spec.session_id) == _text(parent.session_id)
                        and _text(spec.operator_session_id or spec.session_id)
                        == _text(parent.operator_session_id or parent.session_id)
                        and _text(spec.goal_id) == _text(parent.goal_id)
                        and type(spec.goal_revision) is int
                        and int(spec.goal_revision) == int(parent.goal_revision)
                        and type(spec.parent_fencing_token) is int
                        and type(spec.max_outstanding_jobs) is int
                        and (
                            spec.max_outstanding_jobs
                            == (_canonical_goal_max_outstanding(canonical_goal) or 1)
                        )
                        and _text(parent.job_kind) == "guardian_routine_v2"
                        and _text(parent.capability_version) == "guardian-routine.v2"
                    )
                    if not native_procedure_leaf:
                        raise DurableJobLeaseError("native leaf parent context is incomplete")
            if spec.routine_publication_admission_guard is not None:
                if dialect_name == "sqlite" and not transaction_started:
                    # A guard is meaningful even for a goalless internal row.
                    # Start the same immediate transaction before reading the
                    # parent/child so the final insert cannot follow a stale
                    # cancellation preflight.
                    await db.execute(text("BEGIN IMMEDIATE"))
                    transaction_started = True
                await self._assert_routine_publication_admission_guard(
                    db,
                    spec=spec,
                    guard=spec.routine_publication_admission_guard,
                    now=now,
                    dialect_name=dialect_name,
                )
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

            if (
                spec.goal_id is not None
                and spec.max_outstanding_jobs is not None
                and not native_procedure_leaf
            ):
                # This count and the child insert share the same durable
                # transaction.  Unlike the scheduler's advisory listing,
                # this canonical admission fence cannot be bypassed by the
                # list limit or by a second scheduler occurrence.
                outstanding = await db.execute(
                    select(func.count(WorkflowRunState.run_identity)).where(
                        WorkflowRunState.goal_id == spec.goal_id,
                        # A workflow step is bounded by its admitted root and
                        # must not consume a second goal-level outstanding
                        # slot. The parent tree still owns child cancellation
                        # and fencing independently.
                        WorkflowRunState.parent_job_id.is_(None),
                        WorkflowRunState.parent_run_identity.is_(None),
                        # Failed/blocked rows can be explicitly resumed back
                        # to queued, so they remain outstanding until the
                        # run reaches a terminal succeeded/degraded/cancelled
                        # state.
                        WorkflowRunState.status.not_in(tuple(DURABLE_JOB_TERMINAL_STATUSES)),
                        WorkflowRunState.record_schema_version
                        >= DURABLE_JOB_RECORD_SCHEMA_VERSION,
                    )
                )
                if int(outstanding.scalar_one() or 0) >= int(spec.max_outstanding_jobs):
                    raise DurableJobAdmissionDenied(
                        "goal_budget_outstanding_limit",
                        goal_id=spec.goal_id,
                    )

            by_id = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity.job_id))
            ).scalars().first()
            if by_id is not None:
                raise DurableJobIdempotencyConflict("job_id already belongs to a different invocation")
            status = "failed" if deadline and deadline <= now else "accepted"
            failure_reason = "deadline_expired" if status == "failed" else None
            run = WorkflowRunState(
                run_identity=identity.job_id,
                root_run_identity=root_run_identity,
                parent_run_identity=spec.parent_job_id,
                parent_job_id=spec.parent_job_id,
                parent_fencing_token=spec.parent_fencing_token,
                workflow_name=identity.job_kind,
                tool_name=identity.job_kind,
                session_id=spec.session_id,
                conversation_id=spec.conversation_id or spec.session_id,
                operator_session_id=spec.operator_session_id,
                status=status,
                branch_depth=branch_depth,
                run_fingerprint=run_fingerprint,
                arguments_json=_canonical(safe_inputs),
                approval_context_json=_canonical(safe_authority),
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
                budget_digest=budget_digest,
                idempotency_scope=identity.idempotency_scope,
                idempotency_key=identity.idempotency_key,
                idempotency_binding=binding,
                priority=int(spec.priority),
                dependencies_json=_canonical(_string_list(spec.dependencies)),
                resource_claims_json=_canonical(_string_list(spec.resource_claims)),
                declared_authority_json=_canonical(safe_authority),
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

    async def get_jobs(self, job_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Read a bounded set of typed jobs in one readonly session."""

        bounded_ids = list(dict.fromkeys(str(item).strip() for item in job_ids if str(item).strip()))[:100]
        if not bounded_ids:
            return {}
        async with self._session() as db:
            runs = (
                await db.execute(
                    select(WorkflowRunState).where(WorkflowRunState.run_identity.in_(bounded_ids))
                )
            ).scalars().all()
            result: dict[str, dict[str, Any]] = {}
            for run in runs:
                db.expunge(run)
                result[str(run.run_identity)] = _serialize(run)
            return result

    async def assert_active_lease(
        self,
        job_id: str,
        *,
        owner: str,
        fencing_token: int,
    ) -> dict[str, Any]:
        """Re-read a running job and reject a stale or expired lease."""
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status != "running":
                raise DurableJobLeaseError("durable job is not running")
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            db.expunge(run)
            return _serialize(run)

    async def get_by_idempotency_binding(
        self,
        *,
        owner_principal_id: str,
        goal_id: str | None,
        goal_revision: int | None,
        idempotency_scope: str,
        idempotency_key: str,
        expected_job_id: str | None = None,
        owner_kind: str | None = None,
        service_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        job_kind: str | None = None,
        capability_version: str | None = None,
        input_digest: str | None = None,
        authority_digest: str | None = None,
        run_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Read the exact durable admission for one immutable binding.

        Restart recovery must look up the binding persisted by admission. A
        reconstructed job id is only an advisory hint and cannot prove that a
        durable run belongs to the board attempt. The binding calculation is
        the same canonical function used by ``admit_job``.
        """

        owner_principal_id = _bounded_identifier(
            owner_principal_id,
            field_name="owner_principal_id",
        )
        idempotency_scope = _bounded_identifier(
            idempotency_scope,
            field_name="idempotency_scope",
        )
        idempotency_key = _bounded_identifier(
            idempotency_key,
            field_name="idempotency_key",
        )
        expected_job_id = _bounded_identifier(expected_job_id, field_name="expected_job_id") or None
        owner_kind = _bounded_identifier(owner_kind, field_name="owner_kind") or None
        service_id = _bounded_identifier(service_id, field_name="service_id") or None
        session_id = _bounded_identifier(session_id, field_name="session_id") or None
        operator_session_id = (
            _bounded_identifier(operator_session_id, field_name="operator_session_id") or None
        )
        job_kind = _bounded_identifier(job_kind, field_name="job_kind") or None
        capability_version = (
            _bounded_identifier(capability_version, field_name="capability_version") or None
        )
        input_digest = _bounded_identifier(input_digest, field_name="input_digest") or None
        authority_digest = _bounded_identifier(authority_digest, field_name="authority_digest") or None
        run_fingerprint = _bounded_identifier(run_fingerprint, field_name="run_fingerprint") or None
        binding = _binding(
            owner_principal_id=owner_principal_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            idempotency_scope=idempotency_scope,
            dedupe_key=idempotency_key,
        )
        async with self._session() as db:
            run = (
                await db.execute(
                    select(WorkflowRunState).where(
                        WorkflowRunState.idempotency_binding == binding,
                    )
                )
            ).scalars().first()
            if run is None:
                return None
            # The binding includes the immutable fields above, and these
            # explicit checks keep a malformed legacy row from being treated
            # as a recovery match merely because its digest collides.
            if (
                run.idempotency_binding != binding
                or run.owner_principal_id != owner_principal_id
                or run.goal_id != goal_id
                or run.goal_revision != goal_revision
                or run.idempotency_scope != idempotency_scope
                or run.idempotency_key != idempotency_key
                or (expected_job_id is not None and run.run_identity != expected_job_id)
                or (owner_kind is not None and run.owner_kind != owner_kind)
                or (service_id is not None and run.service_id != service_id)
                or (session_id is not None and run.session_id != session_id)
                or (
                    operator_session_id is not None
                    and run.operator_session_id != operator_session_id
                )
                or (job_kind is not None and run.job_kind != job_kind)
                or (
                    capability_version is not None
                    and run.capability_version != capability_version
                )
                or (input_digest is not None and run.input_digest != input_digest)
                or (authority_digest is not None and run.authority_digest != authority_digest)
                or (
                    run_fingerprint is not None
                    and run.run_fingerprint != run_fingerprint
                )
            ):
                raise DurableJobIdempotencyConflict(
                    "durable admission binding conflicts on immutable identity"
                )
            db.expunge(run)
            return _serialize(run)

    async def bind_approval_id(
        self,
        job_id: str,
        approval_id: str,
        *,
        owner: str,
        fencing_token: int,
        expected_revision: int | None = None,
        repo_node_posture_expectation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind the durable approval row to a leased job exactly once.

        Approval requests are created after a worker has inspected the local
        workspace, so their database id is not known at initial job admission.
        This narrow CAS fills that one field before the job enters
        ``awaiting_approval``.  It cannot replace an existing binding or be
        called without the current execution lease.
        """
        approval_id = _bounded_identifier(approval_id, field_name="approval_id")
        if not approval_id:
            raise DurableJobTransitionError("approval id is required")
        owner = _text(owner)
        if not owner or fencing_token is None:
            raise DurableJobLeaseError("approval binding requires owner and fencing token")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            current = _json_load(getattr(run, "declared_authority_json", None), {})
            if not isinstance(current, Mapping):
                raise DurableJobTransitionError("durable authority metadata is malformed")
            if current.get("sandbox_profile") == "repo-node24-npm-v1":
                # Check the original complete outer authority before adding
                # an approval id; a recomputed inner hash cannot repair drift.
                if _digest(current) != run.authority_digest:
                    raise DurableJobIdempotencyConflict("Node durable authority digest changed")
                _safe_durable_authority(current, repo_node_posture_expectation=repo_node_posture_expectation)
            existing_id = _authority_approval_id(current)
            if existing_id:
                if existing_id == approval_id:
                    db.expunge(run)
                    return _serialize(
                        run,
                        receipt={
                            "kind": "approval_binding",
                            "status": "deduped",
                            "approval_id": approval_id,
                            "revision": _revision(run),
                            "operator_visible": True,
                        },
                    )
                raise DurableJobIdempotencyConflict(
                    "durable job already has a different approval binding"
                )
            if str(run.status) != "running":
                raise DurableJobTransitionError(
                    f"approval binding requires a running job (current={run.status})"
                )
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            now = _utc_now()
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            bound_authority = dict(current)
            bound_authority["approval_id"] = approval_id
            safe_bound_authority = _safe_durable_authority(
                bound_authority, repo_node_posture_expectation=repo_node_posture_expectation,
            )
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == "running",
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.lease_owner == owner,
                WorkflowRunState.fencing_token == fencing_token,
                WorkflowRunState.lease_expires_at > now,
            ]
            _append_parent_fence_condition(conditions, run, now=now)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    declared_authority_json=_canonical(safe_bound_authority),
                    approval_context_json=_canonical(safe_bound_authority),
                    authority_digest=_digest(bound_authority),
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("durable job changed before approval binding")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "approval_binding",
                "status": "recorded",
                "approval_id": approval_id,
                "authority_digest": refreshed.authority_digest,
                "revision": _revision(refreshed),
                "fencing_token": refreshed.fencing_token,
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

    async def list_jobs(
        self,
        *,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List canonical typed jobs for operator projections and recovery.

        The workflows API historically queried ``WorkflowStateRepository``;
        that serializer intentionally owns only legacy step rows.  Keep this
        read path beside the typed writes so schema-v2 receipt ledgers are
        projected without asking the legacy repository to mutate or interpret
        them.
        """
        bounded_limit = max(1, min(int(limit), 100))
        async with self._session() as db:
            stmt = (
                select(WorkflowRunState)
                .where(WorkflowRunState.record_schema_version >= DURABLE_JOB_RECORD_SCHEMA_VERSION)
                .order_by(WorkflowRunState.updated_at.desc())
                .limit(bounded_limit)
            )
            if session_id:
                stmt = stmt.where(WorkflowRunState.session_id == session_id)
            runs = (await db.execute(stmt)).scalars().all()
            serialized: list[dict[str, Any]] = []
            for run in runs:
                db.expunge(run)
                serialized.append(_serialize(run))
            return serialized

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
        terminal_authority_check: Callable[[Any, Any], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if to_status not in DURABLE_JOB_STATUSES:
            raise DurableJobTransitionError(f"unknown durable job status: {to_status}")
        async with self._session() as db:
            # A capability-specific terminal guard must observe its owner,
            # consent, and artifact rows in the same serialized transaction as
            # the root CAS.  SQLite otherwise permits a stale read snapshot
            # between the caller's last preflight and this transition.
            if terminal_authority_check is not None and to_status in {"succeeded", "degraded"}:
                bind = db.get_bind()
                dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
                if dialect_name == "sqlite":
                    await db.execute(text("BEGIN IMMEDIATE"))
            run = await self._fetch(db, job_id)
            if to_status not in {"failed", "cancelled"}:
                await _assert_canonical_goal_fence(
                    db,
                    goal_id=getattr(run, "goal_id", None),
                    goal_revision=getattr(run, "goal_revision", None),
                    owner_kind=_text(getattr(run, "owner_kind", None)),
                    owner_principal_id=getattr(run, "owner_principal_id", None),
                    session_id=getattr(run, "session_id", None),
                    authority=getattr(run, "declared_authority_json", None),
                )
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
            if current in {"blocked", "failed"} and to_status == "queued" and _cleanup_reservation_pending(run):
                raise DurableJobTransitionError(
                    "private artifact cleanup is reserved; reconcile the exact cleanup receipt before resume"
                )
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
            approval_request_record: dict[str, Any] | None = None
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
                if _job_has_unsafe_effects(effect_ledger):
                    raise DurableJobTransitionError(
                        "approval-held job retains an unresolved external effect; reconcile before resume"
                    )
                from src.approval.repository import approval_repository

                durable_authority = _json_load(getattr(run, "declared_authority_json", None), {})
                durable_criterion_id = (
                    _text(durable_authority.get("criterion_id"))
                    if isinstance(durable_authority, Mapping)
                    else ""
                ) or None
                approval_request_record = await approval_repository.consume_approved_for_resume(
                    db=db,
                    approval_id=approval_resume_record["approval_id"],
                    owner_operator_session_id=approval_resume_record["operator_session_id"],
                    operator_principal_id=approval_resume_record["operator_principal_id"],
                    job_id=job_id,
                    owner_kind=approval_resume_record["owner_kind"],
                    owner_principal_id=approval_resume_record["owner_principal_id"],
                    service_id=approval_resume_record["service_id"],
                    approval_owner_principal_id=approval_resume_record["operator_principal_id"],
                    authority_digest=approval_resume_record["authority_digest"],
                    goal_id=approval_resume_record["goal_id"],
                    goal_revision=approval_resume_record["goal_revision"],
                    plan_revision=approval_resume_record["plan_revision"],
                    capability_version=approval_resume_record["capability_version"],
                    budget_digest=approval_resume_record["budget_digest"],
                    expires_at=approval_resume_record["expires_at"],
                    # The approval row is owned by the authenticated
                    # operator's conversation.  Service-owned runs carry a
                    # different execution session, so lookup must use the
                    # explicit approval owner session while the immutable
                    # execution binding remains in the row details.
                    session_id=approval_resume_record["operator_session_id"],
                    conversation_id=approval_resume_record["operator_session_id"],
                    criterion_id=durable_criterion_id,
                    candidate_id=getattr(run, "candidate_id", None),
                )
                if approval_request_record is None and _text(getattr(run, "session_id", None)) != _text(
                    approval_resume_record["operator_session_id"]
                ):
                    # Generic durable workflows historically stored the
                    # approval row under the execution session while keeping
                    # the authenticated approver in ``operator_session_id``.
                    # Native service-owned workflows use the approver's
                    # session for the row itself. Preserve both layouts while
                    # requiring the same owner, operator, authority, and
                    # durable binding checks in either path.
                    approval_request_record = await approval_repository.consume_approved_for_resume(
                        db=db,
                        approval_id=approval_resume_record["approval_id"],
                        owner_operator_session_id=approval_resume_record["operator_session_id"],
                        operator_principal_id=approval_resume_record["operator_principal_id"],
                        job_id=job_id,
                        owner_kind=approval_resume_record["owner_kind"],
                        owner_principal_id=approval_resume_record["owner_principal_id"],
                        service_id=approval_resume_record["service_id"],
                        # Legacy generic rows use the durable service owner in
                        # the ApprovalRequest owner column; the interactive
                        # operator remains bound by its dedicated detail and
                        # operator-session fields.
                        approval_owner_principal_id=None,
                        authority_digest=approval_resume_record["authority_digest"],
                        goal_id=approval_resume_record["goal_id"],
                        goal_revision=approval_resume_record["goal_revision"],
                        plan_revision=approval_resume_record["plan_revision"],
                        capability_version=approval_resume_record["capability_version"],
                        budget_digest=approval_resume_record["budget_digest"],
                        expires_at=approval_resume_record["expires_at"],
                        session_id=run.session_id,
                        conversation_id=getattr(run, "conversation_id", None) or run.session_id,
                        criterion_id=durable_criterion_id,
                        candidate_id=getattr(run, "candidate_id", None),
                    )
                if approval_request_record is None:
                    raise DurableJobTransitionError(
                        "approval resume requires the current authenticated ApprovalRequest"
                    )
                approval_resume_record["approval_request_status"] = approval_request_record.get("status")
                approval_resume_record["approval_request_fingerprint"] = approval_request_record.get(
                    "fingerprint"
                )
            if current == "failed" and to_status == "queued":
                raise DurableJobTransitionError(
                    "failed jobs require explicit retry with reconciliation"
                )
            if current == "failed" and to_status == "cancelled":
                try:
                    effect_ledger = _effect_ledger_or_raise(run.effect_receipts_json)
                except DurableJobTransitionError:
                    to_status = "unknown_external_effect"
                    reason = reason or "malformed_effect_history_requires_reconciliation"
                if effect_ledger is not None and _job_has_unsafe_effects(effect_ledger):
                    to_status, recovery_reason = _effect_recovery_state(effect_ledger)
                    reason = reason or f"{recovery_reason}_pending_before_transition"
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
                "degraded",
                "succeeded",
            }:
                try:
                    effect_ledger = _effect_ledger_or_raise(run.effect_receipts_json)
                except DurableJobTransitionError:
                    if to_status in {"succeeded", "degraded"}:
                        raise
                    to_status = "blocked"
                    reason = "malformed_effect_history_requires_reconciliation"
                if effect_ledger is not None and _job_has_unsafe_effects(effect_ledger):
                    if to_status == "cancelled":
                        to_status, recovery_reason = _effect_recovery_state(effect_ledger)
                        reason = reason or f"{recovery_reason}_pending_before_transition"
            if to_status in {"succeeded", "degraded"}:
                if _deadline_expired(run):
                    raise DurableJobTransitionError("job deadline has expired")
                effect_ledger = effect_ledger or _effect_ledger_or_raise(run.effect_receipts_json)
                if _job_has_unsafe_effects(effect_ledger):
                    raise DurableJobTransitionError(
                        "cannot mark durable job terminal: unresolved external effect"
                    )
                if not _verified_readback_exists(effect_ledger):
                    raise DurableJobTransitionError(
                        "cannot mark durable job terminal: verified capability readback is required"
                    )
                if terminal_authority_check is not None:
                    await terminal_authority_check(db, run)
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
                "degraded",
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
            if approval_request_record is not None:
                # The native bounded runner needs the short-lived repository
                # binding issued by the same approval CAS.  Keep it in the
                # in-process transition receipt; the durable effect ledger
                # stores only the redacted approval-resume record.
                receipt["approval_binding"] = approval_request_record
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

    async def cancel_job_tree(
        self,
        root_job_id: str,
        *,
        reason: str = "operator_cancelled",
    ) -> list[dict[str, Any]]:
        """Cancel a durable root and its persisted descendants safely.

        Descendants are cancelled before their parent so a child cannot keep
        running after the board has requested cancellation. Each transition
        goes through the existing lease/revision/fencing CAS; unresolved
        external effects remain in their authoritative unknown state and are
        returned for operator reconciliation.
        """

        root_job_id = _text(root_job_id)
        if not root_job_id:
            raise DurableJobNotFound("<empty>")
        async with self._session() as db:
            # Current admissions share ``root_run_identity``.  Older durable
            # rows can still be active with a self-root, however, so walk the
            # persisted parent links as a bounded frontier as well.  The
            # parent link is the exact tree edge; a different root's rows do
            # not enter this set merely because their root identity is close
            # in text or ordering.
            runs_by_id: dict[str, WorkflowRunState] = {}
            frontier = {root_job_id}
            while frontier:
                result = await db.execute(
                    select(WorkflowRunState).where(
                        or_(
                            WorkflowRunState.run_identity.in_(frontier),
                            WorkflowRunState.root_run_identity.in_(frontier),
                            WorkflowRunState.parent_run_identity.in_(frontier),
                        )
                    )
                )
                next_frontier: set[str] = set()
                for run in result.scalars().all():
                    run_identity = _text(run.run_identity)
                    if not run_identity or run_identity in runs_by_id:
                        continue
                    runs_by_id[run_identity] = run
                    next_frontier.add(run_identity)
                frontier = next_frontier
            runs = list(runs_by_id.values())
        if not runs:
            raise DurableJobNotFound(root_job_id)

        # Use the live parent links to order legacy rows whose persisted
        # branch_depth was never populated.  Falling back to the durable depth
        # keeps a row with incomplete legacy ancestry cancellable while still
        # making descendants precede their parent whenever the edge is known.
        def _tree_depth(run: WorkflowRunState) -> int:
            current_id = _text(run.run_identity)
            depth = 0
            visited: set[str] = set()
            while current_id and current_id != root_job_id:
                if current_id in visited:
                    break
                visited.add(current_id)
                current = runs_by_id.get(current_id)
                parent_id = _text(getattr(current, "parent_run_identity", None)) if current else ""
                if not parent_id or parent_id not in runs_by_id:
                    break
                depth += 1
                current_id = parent_id
            if depth:
                return depth
            return max(0, int(getattr(run, "branch_depth", 0) or 0))

        runs.sort(
            key=lambda run: (
                _tree_depth(run),
                0 if run.run_identity == root_job_id else 1,
            ),
            reverse=True,
        )
        receipts: list[dict[str, Any]] = []
        for run in runs:
            current = await self.get_job(run.run_identity)
            if not isinstance(current, Mapping):
                continue
            status = _text(current.get("status"))
            if status in DURABLE_JOB_TERMINAL_STATUSES:
                receipts.append(dict(current))
                continue
            # An unresolved effect cannot be converted into a successful
            # cancellation claim. The normal transition classifier preserves
            # the unknown/cost state for running jobs; skip already-unknown
            # rows so no caller can erase their liability.
            if status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES:
                receipts.append(dict(current))
                continue
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            kwargs: dict[str, Any] = {
                "expected_revision": current.get("revision"),
                "reason": reason,
            }
            if status == "running":
                owner = _text(lease.get("owner"))
                fence = lease.get("fencing_token")
                if not owner or fence is None:
                    receipts.append(dict(current))
                    continue
                kwargs.update(owner=owner, fencing_token=int(fence))
            try:
                cancelled = await self.cancel_job(run.run_identity, **kwargs)
            except DurableJobError:
                # A concurrent worker/recovery pass won the CAS. Re-read the
                # durable projection and report that authoritative outcome.
                cancelled = await self.get_job(run.run_identity)
            if isinstance(cancelled, Mapping):
                receipts.append(dict(cancelled))
        return receipts

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
        current = await self.get_job(job_id)
        if current is not None and current.get("status") != "awaiting_approval":
            # Check the one-shot approval capability before the generic CAS
            # state error.  A replay after a successful resume must make the
            # consumed ApprovalRequest visible to the operator rather than
            # looking like an unexplained revision race.
            raise DurableJobTransitionError(
                "approval resume requires the current authenticated ApprovalRequest"
            )
        receipt_fields = dict(approval_receipt)
        if _text(receipt_fields.get("status")) != "approved" or receipt_fields.get("authenticated") is not True:
            raise DurableJobTransitionError(
                "approval resume requires a current authenticated approval"
            )
        if receipt_fields.get("revoked") is True:
            raise DurableJobTransitionError("approval resume approval has been revoked")
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
        continue_existing_attempt: bool = False,
    ) -> dict[str, Any]:
        owner = _text(owner)
        if not owner:
            raise DurableJobLeaseError("owner is required to claim a job")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        if expected_state != "queued":
            raise DurableJobTransitionError("durable job claims require the queued state")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
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
                persisted_deadline = _as_utc(run.deadline_at)
            except ValueError as exc:
                raise DurableJobTransitionError("job deadline metadata is malformed") from exc
            if persisted_deadline and persisted_deadline <= now:
                deadline_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                    or_(
                        WorkflowRunState.lease_owner.is_(None),
                        WorkflowRunState.lease_expires_at <= now,
                    ),
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
            expires = now + timedelta(seconds=int(lease_seconds))
            if persisted_deadline is not None and expires > persisted_deadline:
                expires = persisted_deadline
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
            accounting_resume = False
            if run.attempt_count >= run.max_attempts and not continue_existing_attempt:
                accounting_resume = await self._accounting_resume_claim_allowed(db, run)
            if continue_existing_attempt:
                # An operator-approved pause is a continuation of the same
                # durable attempt.  It must reacquire a fresh execution lease
                # and fence while preserving the attempt budget; otherwise a
                # max_attempts=1 job would be consumed merely by crossing its
                # explicit review boundary.  Callers must prove that an
                # attempt already existed; a fresh queued row still uses the
                # normal claim path below.
                if int(run.attempt_count or 0) <= 0:
                    raise DurableJobTransitionError(
                        "existing-attempt continuation requires a prior claim"
                    )
                if int(run.attempt_count or 0) > int(run.max_attempts or 0):
                    raise DurableJobTransitionError("existing-attempt continuation exceeds attempt budget")
            elif run.attempt_count >= run.max_attempts and not accounting_resume:
                raise DurableJobTransitionError("attempt budget exhausted")
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == expected_state,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.fencing_token == expected_fence,
                or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= now),
            ]
            _append_parent_fence_condition(conditions, run, now=now)
            claim_values: dict[str, Any] = {
                "status": "running",
                "lease_owner": owner,
                "lease_expires_at": expires,
                "fencing_token": WorkflowRunState.fencing_token + 1,
                "revision": WorkflowRunState.revision + 1,
                "heartbeat_at": now,
                "updated_at": now,
            }
            if not continue_existing_attempt and not accounting_resume:
                claim_values["attempt_count"] = WorkflowRunState.attempt_count + 1
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(**claim_values)
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
            try:
                persisted_deadline = _as_utc(run.deadline_at)
            except ValueError as exc:
                raise DurableJobLeaseError("job deadline metadata is malformed") from exc
            if persisted_deadline is not None and expires > persisted_deadline:
                expires = persisted_deadline
            if expires <= now:
                raise DurableJobLeaseError("job deadline has expired")
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
            try:
                persisted_deadline = _as_utc(run.deadline_at)
            except ValueError as exc:
                raise DurableJobLeaseError("job deadline metadata is malformed") from exc
            if persisted_deadline is not None and persisted_deadline <= now:
                raise DurableJobTransitionError("job deadline has expired")
            if run.lease_owner != old_owner:
                raise DurableJobLeaseError("lease source owner does not match")
            if expiry is not None and expiry > now:
                raise DurableJobLeaseError("active lease cannot be transferred before expiry")
            transferred_expiry = now + timedelta(seconds=int(lease_seconds))
            if persisted_deadline is not None and transferred_expiry > persisted_deadline:
                transferred_expiry = persisted_deadline
            if transferred_expiry <= now:
                raise DurableJobTransitionError("job deadline has expired")
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
                    lease_expires_at=transferred_expiry,
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
        checkpoint_payload: Any | None = None,
        owner: str,
        fencing_token: int,
        safe: bool = True,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not _text(checkpoint_id):
            raise ValueError("checkpoint_id is required")
        async with self._session() as db:
            # SQLite WAL readers cannot reliably upgrade a snapshot to a
            # writer while another dispatcher is committing.  Acquire the
            # same immediate writer boundary used by durable admission and
            # repair-capacity reservation before reading the row, preserving
            # the existing fenced CAS below without masking a real conflict.
            bind = db.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", "") == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            run = await self._fetch(db, job_id)
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
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
            if safe and checkpoint_payload is not None:
                # A caller that has already passed its capability-specific
                # checkpoint policy may retain a bounded, JSON-safe payload
                # for recovery.  The legacy/default path remains digest-only.
                receipt["payload"] = _safe_structure(checkpoint_payload)
            # Repair reservation history is part of the physical execution
            # fence.  Do not let the generic checkpoint writer treat malformed
            # JSON as an empty legacy history and evict that fence.
            if self._repo_repair_claimed(run):
                self._repo_repair_reservation_state(run)
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
                    checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts(existing)),
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

    @staticmethod
    def _repo_repair_reservation_state(run: WorkflowRunState) -> dict[str, Any] | None:
        """Read the latest exact repair reservation marker from checkpoints."""

        raw = getattr(run, "checkpoint_receipts_json", None)
        if raw is None or not str(raw).strip():
            checkpoints = []
        else:
            try:
                checkpoints = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise DurableJobTransitionError(
                    "repository repair reservation history is malformed"
                ) from exc
        if not isinstance(checkpoints, list):
            raise DurableJobTransitionError("repository repair reservation history is malformed")
        for item in reversed(checkpoints):
            if not isinstance(item, Mapping):
                continue
            checkpoint_id = _text(item.get("checkpoint_id"))
            if checkpoint_id not in {
                "repo-repair-execution-reservation",
                "repo-repair-execution-release",
            }:
                continue
            payload = item.get("payload")
            if not isinstance(payload, Mapping) or _text(payload.get("kind")) != "repo_repair_execution_reservation":
                raise DurableJobTransitionError("repository repair reservation marker is malformed")
            status = _text(payload.get("status"))
            if status not in {"held", "released"}:
                raise DurableJobTransitionError("repository repair reservation status is malformed")
            try:
                fence = int(payload.get("fence"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise DurableJobTransitionError("repository repair reservation fence is malformed") from exc
            if (
                not _text(payload.get("job_id"))
                or not _text(payload.get("attempt_id"))
                or fence <= 0
                or not _text(payload.get("authority_digest"))
            ):
                raise DurableJobTransitionError("repository repair reservation identity is malformed")
            if status == "released" and payload.get("readback_scope") == "process_cleanup_only":
                declared = _json_load(run.declared_authority_json, {})
                if (run.job_kind != "engineering.repo-repair.v1" or declared.get("sandbox_profile") != "repo-node24-npm-v1"
                    or payload.get("cleanup_receipt_verified") is not True or payload.get("cleanup_proven") is not True
                    or _text(payload.get("outcome_status")) != "unknown_external_effect"
                    or re.fullmatch(r"[0-9a-f]{64}", _text(payload.get("process_cleanup_readback_sha256"))) is None):
                    raise DurableJobTransitionError("repository repair process cleanup release proof is malformed")
            elif status == "released" and (
                payload.get("cleanup_proven") is not True
                or payload.get("readback_verified") is not True
                or _text(payload.get("outcome_status")) not in {"succeeded", "degraded", "failed", "cancelled"}
            ):
                raise DurableJobTransitionError("repository repair release proof is malformed")
            return dict(payload)
        return None

    @staticmethod
    def _repo_repair_claimed(run: WorkflowRunState) -> bool:
        raw = getattr(run, "resource_claims_json", None)
        if raw is None or not str(raw).strip():
            return False
        try:
            claims = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableJobTransitionError("repository repair resource claims are malformed") from exc
        if not isinstance(claims, list):
            raise DurableJobTransitionError("repository repair resource claims are malformed")
        return isinstance(claims, list) and "repo-repair-execution" in {
            _text(value) for value in claims
        }

    @staticmethod
    def _repo_repair_reservation_matches(
        payload: Mapping[str, Any] | None,
        *,
        job_id: str,
        attempt_id: str,
        fence: int,
        authority_digest: str,
    ) -> bool:
        if not isinstance(payload, Mapping):
            return False
        try:
            observed_fence = int(payload.get("fence"))
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            _text(payload.get("job_id")) == job_id
            and _text(payload.get("attempt_id")) == attempt_id
            and observed_fence == fence
            and _text(payload.get("authority_digest")) == authority_digest
        )

    async def reserve_repo_repair_execution(
        self,
        job_id: str,
        *,
        owner: str,
        fencing_token: int,
        attempt_id: str,
        authority_digest: str,
        execution_deadline_at: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve the one physical repair execution slot.

        The flock is acquired by the dispatcher before this method.  SQLite's
        immediate transaction makes the durable reservation the final race
        authority across dispatcher processes and preserves it after process
        death.  A reservation is idempotent only for the exact same job,
        attempt, fence, and authority digest.
        """

        job_id = _text(job_id)
        owner = _text(owner)
        attempt_id = _text(attempt_id)
        authority_digest = _text(authority_digest)
        try:
            fencing_token = int(fencing_token)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DurableJobLeaseError("repository repair reservation fence is malformed") from exc
        if not job_id or not owner or not attempt_id or not authority_digest or fencing_token <= 0:
            raise DurableJobLeaseError("repository repair reservation identity is incomplete")
        async with self._session() as db:
            bind = db.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect_name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            run = await self._fetch(db, job_id)
            if run.status != "running":
                raise DurableJobAdmissionDenied(
                    "repo_repair_execution_job_not_running",
                    goal_id=_text(getattr(run, "goal_id", None)) or None,
                )
            if not self._repo_repair_claimed(run):
                raise DurableJobTransitionError("repository repair execution claim is missing")
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            current = self._repo_repair_reservation_state(run)
            if current is not None and _text(current.get("status")) == "held":
                if self._repo_repair_reservation_matches(
                    current,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    fence=fencing_token,
                    authority_digest=authority_digest,
                ):
                    db.expunge(run)
                    return _serialize(
                        run,
                        receipt={
                            "kind": "repo_repair_execution_reservation",
                            "status": "deduped",
                            "job_id": job_id,
                            "attempt_id": attempt_id,
                            "fence": fencing_token,
                            "authority_digest": authority_digest,
                            "execution_deadline_at": current.get("execution_deadline_at"),
                            "operator_visible": True,
                        },
                    )
                raise DurableJobAdmissionDenied(
                    "repo_repair_execution_busy",
                    goal_id=_text(getattr(run, "goal_id", None)) or None,
                )
            if current is not None and _text(current.get("status")) == "released":
                if self._repo_repair_reservation_matches(
                    current,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    fence=fencing_token,
                    authority_digest=authority_digest,
                ):
                    raise DurableJobTransitionError("repository repair execution reservation already settled")
                raise DurableJobAdmissionDenied(
                    "repo_repair_execution_busy",
                    goal_id=_text(getattr(run, "goal_id", None)) or None,
                )

            rows = (
                await db.execute(
                    select(WorkflowRunState).where(
                        WorkflowRunState.record_schema_version >= DURABLE_JOB_RECORD_SCHEMA_VERSION,
                        WorkflowRunState.run_identity != job_id,
                        WorkflowRunState.job_kind == "engineering.repo-repair.v1",
                        # Keep the BEGIN IMMEDIATE section bounded to the
                        # repair resource.  The Python pass below still
                        # validates the canonical JSON claim exactly.
                        WorkflowRunState.resource_claims_json.contains("repo-repair-execution"),
                        WorkflowRunState.checkpoint_receipts_json.contains(
                            "repo_repair_execution_reservation"
                        ),
                    )
                )
            ).scalars().all()
            for other in rows:
                if not self._repo_repair_claimed(other):
                    continue
                other_reservation = self._repo_repair_reservation_state(other)
                if isinstance(other_reservation, Mapping) and _text(other_reservation.get("status")) == "held":
                    raise DurableJobAdmissionDenied(
                        "repo_repair_execution_busy",
                        goal_id=_text(getattr(run, "goal_id", None)) or None,
                    )

            payload = {
                "kind": "repo_repair_execution_reservation",
                "status": "held",
                "job_id": job_id,
                "attempt_id": attempt_id,
                "fence": fencing_token,
                "authority_digest": authority_digest,
                "execution_deadline_at": execution_deadline_at,
                "operator_visible": True,
                "recorded_at": _utc_now().isoformat(),
            }
            existing = _json_load(run.checkpoint_receipts_json, [])
            existing = [
                item
                for item in existing
                if isinstance(item, Mapping)
                and _text(item.get("checkpoint_id")) != "repo-repair-execution-reservation"
            ]
            receipt = {
                "checkpoint_id": "repo-repair-execution-reservation",
                "state_digest": _digest(payload),
                "state_keys": sorted(payload),
                "safe": True,
                "recorded_at": _utc_now().isoformat(),
                "fence": fencing_token,
                "payload": _safe_structure(payload),
            }
            existing.append(receipt)
            now = _utc_now()
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == "running",
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == fencing_token,
                    WorkflowRunState.lease_owner == owner,
                    WorkflowRunState.lease_expires_at > now,
                )
                .values(
                    checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts(existing)),
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("repository repair reservation fence is stale")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(
                refreshed,
                receipt={
                    "kind": "repo_repair_execution_reservation",
                    "status": "held",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "fence": fencing_token,
                    "authority_digest": authority_digest,
                    "execution_deadline_at": execution_deadline_at,
                    "operator_visible": True,
                },
            )

    async def settle_repo_repair_execution(
        self,
        job_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        authority_digest: str,
        cleanup_proven: bool,
        readback_verified: bool,
        outcome_status: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Publish reservation release only after exact cleanup/readback proof."""

        if not cleanup_proven or not readback_verified:
            raise DurableJobTransitionError("repository repair cleanup/readback is not proven")
        if outcome_status not in {"succeeded", "degraded", "failed", "cancelled"}:
            raise DurableJobTransitionError("repository repair outcome is not terminal")
        try:
            fencing_token = int(fencing_token)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DurableJobLeaseError("repository repair release fence is malformed") from exc
        job_id = _text(job_id)
        attempt_id = _text(attempt_id)
        authority_digest = _text(authority_digest)
        async with self._session() as db:
            bind = db.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", "") == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            run = await self._fetch(db, job_id)
            if run.status not in {"succeeded", "degraded", "failed", "cancelled"}:
                raise DurableJobTransitionError("repository repair job is not terminally published")
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            current = self._repo_repair_reservation_state(run)
            if current is None:
                raise DurableJobTransitionError("repository repair reservation is missing")
            if not self._repo_repair_reservation_matches(
                current,
                job_id=job_id,
                attempt_id=attempt_id,
                fence=fencing_token,
                authority_digest=authority_digest,
            ):
                raise DurableJobLeaseError("repository repair reservation identity changed")
            if _text(current.get("status")) == "released":
                db.expunge(run)
                return _serialize(
                    run,
                    receipt={
                        "kind": "repo_repair_execution_release",
                        "status": "deduped",
                        "job_id": job_id,
                        "attempt_id": attempt_id,
                        "fence": fencing_token,
                        "operator_visible": True,
                    },
                )
            payload = {
                "kind": "repo_repair_execution_reservation",
                "status": "released",
                "job_id": job_id,
                "attempt_id": attempt_id,
                "fence": fencing_token,
                "authority_digest": authority_digest,
                "execution_deadline_at": current.get("execution_deadline_at"),
                "outcome_status": outcome_status,
                "cleanup_proven": True,
                "readback_verified": True,
                "operator_visible": True,
                "recorded_at": _utc_now().isoformat(),
            }
            existing = _json_load(run.checkpoint_receipts_json, [])
            existing = [
                item
                for item in existing
                if isinstance(item, Mapping)
                and _text(item.get("checkpoint_id")) != "repo-repair-execution-release"
            ]
            existing.append(
                {
                    "checkpoint_id": "repo-repair-execution-release",
                    "state_digest": _digest(payload),
                    "state_keys": sorted(payload),
                    "safe": True,
                    "recorded_at": _utc_now().isoformat(),
                    "fence": fencing_token,
                    "payload": _safe_structure(payload),
                }
            )
            now = _utc_now()
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == fencing_token,
                    WorkflowRunState.status.in_(("succeeded", "degraded", "failed", "cancelled")),
                )
                .values(
                    checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts(existing)),
                    updated_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("repository repair release fence is stale")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(
                refreshed,
                receipt={
                    "kind": "repo_repair_execution_release",
                    "status": "released",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "fence": fencing_token,
                    "operator_visible": True,
                },
            )

    async def settle_node_process_cleanup(self, request: NodeProcessCleanupSettlement) -> dict[str, Any]:
        """Release only exact cancelled physical work; retain all task liability."""
        from config.settings import RepoSandboxSettings, settings
        from src.db.models import OperatorSession, RepoRepairProposalRow, RepoRepairSourcePacketRow
        from src.execution.repo_node import NodeRepoRepairExecutor, PROFILE
        from src.workflows.repo_repair import _proposal_authority_payload

        if type(request) is not NodeProcessCleanupSettlement:
            raise DurableJobTransitionError("Node cleanup settlement request is invalid")
        async with self._session() as db:
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            run = await self._fetch(db, request.job_id)
            declared = _json_load(run.declared_authority_json, {})
            if (run.status != "unknown_external_effect" or run.job_kind != "engineering.repo-repair.v1"
                or declared.get("sandbox_profile") != PROFILE or declared.get("executor_kind") != "local"
                or _revision(run) != request.expected_revision
                or run.owner_principal_id != request.owner_principal_id
                or (run.operator_session_id or run.session_id) != request.owner_session_id):
                raise DurableJobLeaseError("Node original cleanup row or revision changed")
            session = await db.get(OperatorSession, request.owner_session_id)
            now = _utc_now()
            if (session is None or session.principal_id != request.owner_principal_id or session.revoked_at is not None
                or session.replaced_by_id is not None or session.is_bearer_tombstone
                or _as_utc(session.idle_expires_at) <= now or _as_utc(session.absolute_expires_at) <= now):
                raise DurableJobTransitionError("Node original operator root is not live")
            proposals = (await db.execute(select(RepoRepairProposalRow).where(
                RepoRepairProposalRow.workflow_run_id == request.job_id,
                RepoRepairProposalRow.owner_principal_id == request.owner_principal_id,
                RepoRepairProposalRow.owner_session_id == request.owner_session_id,
                RepoRepairProposalRow.work_board_task_id == declared.get("task_id"),
                RepoRepairProposalRow.work_board_attempt_id == declared.get("attempt_id"),
            ))).scalars().all()
            if len(proposals) != 1:
                raise DurableJobTransitionError("Node original immutable proposal is missing")
            proposal = proposals[0]
            canonical = _proposal_authority_payload(proposal)
            if (any(request.authority.get(key) != value for key,value in canonical.items() if value not in (None,"",[],{}))
                or proposal.status not in {"approved","consumed","execution_failed","blocked"}
                or str(proposal.goal_id or "") != str(run.goal_id or "")
                or int(proposal.goal_revision) != int(run.goal_revision or 0)):
                raise DurableJobTransitionError("Node original immutable proposal authority changed")
            packet = await db.get(RepoRepairSourcePacketRow, proposal.source_packet_id)
            if (packet is None or packet.state != "verified" or packet.workflow_run_id != request.job_id
                or packet.owner_principal_id != request.owner_principal_id or packet.owner_session_id != request.owner_session_id
                or packet.work_board_task_id != proposal.work_board_task_id or packet.work_board_attempt_id != proposal.work_board_attempt_id
                or packet.base_snapshot_digest != proposal.base_snapshot_digest or packet.source_manifest_digest != proposal.source_digest):
                raise DurableJobTransitionError("Node original source binding changed")
            reservation = self._repo_repair_reservation_state(run)
            if reservation is None or reservation.get("status") != "held":
                raise DurableJobTransitionError("Node physical reservation is not held")
            dispatches = [item for item in _json_load(run.checkpoint_receipts_json, [])
                          if isinstance(item,dict) and isinstance(item.get("payload"),dict)
                          and item["payload"].get("phase") == "executor_dispatch_reserved"]
            if not dispatches:
                raise DurableJobTransitionError("Node original dispatch is missing")
            envelope = dispatches[-1]
            dispatch = {**envelope["payload"], "fencing_token": envelope.get("fencing_token")}
            if (dispatch != dict(request.dispatch) or type(dispatch.get("fencing_token")) is not int
                or dispatch.get("executor_kind") != "local" or dispatch.get("job_id") != request.job_id
                or dispatch.get("authority_digest") != run.authority_digest
                or dispatch.get("attempt") != run.attempt_count or dispatch.get("attempt_id") != proposal.work_board_attempt_id
                or dispatch.get("base_digest") != proposal.base_snapshot_digest
                or request.authority.get("base_digest") != proposal.base_snapshot_digest
                or request.authority.get("profile") != PROFILE or request.authority.get("executor_kind") != "local"
                or request.authority.get("attempt_id") != proposal.work_board_attempt_id
                or request.authority.get("fencing_token") != dispatch["fencing_token"]
                or not self._repo_repair_reservation_matches(reservation, job_id=request.job_id,
                    attempt_id=proposal.work_board_attempt_id, fence=dispatch["fencing_token"], authority_digest=run.authority_digest)):
                raise DurableJobLeaseError("Node original dispatch/reservation changed")
            executor = NodeRepoRepairExecutor(RepoSandboxSettings(profile=PROFILE), workspace_dir=settings.workspace_dir)
            with executor._job_marker_lock(request.job_id):
                digest = executor._process_cleanup_readback_locked(job_id=request.job_id, attempt_id=proposal.work_board_attempt_id,
                    authority_digest=run.authority_digest, fencing_token=dispatch["fencing_token"], authority=request.authority)
                payload = {**reservation, "status":"released", "outcome_status":"unknown_external_effect",
                           "cleanup_proven":True, "cleanup_receipt_verified":True, "process_cleanup_readback_sha256":digest,
                           "readback_scope":"process_cleanup_only", "recorded_at":now.isoformat()}
                history = _json_load(run.checkpoint_receipts_json, [])
                history.append({"checkpoint_id":"repo-repair-execution-release", "state_digest":_digest(payload),
                                "state_keys":sorted(payload), "safe":True, "recorded_at":now.isoformat(),
                                "fence":dispatch["fencing_token"], "payload":payload})
                updated = await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False).where(
                    WorkflowRunState.run_identity == request.job_id, WorkflowRunState.revision == request.expected_revision,
                    WorkflowRunState.fencing_token == run.fencing_token, WorkflowRunState.status == "unknown_external_effect",
                ).values(checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts(history)),
                         updated_at=now, revision=WorkflowRunState.revision + 1))
                if not _rowcount_is_one(updated):
                    raise DurableJobLeaseError("Node physical cleanup CAS is stale")
            refreshed = await self._fetch(db, request.job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind":"repo_repair_process_cleanup_release", **payload})

    async def adopt_routine_publication_child(
        self,
        parent_job_id: str,
        child_job_id: str,
        *,
        parent_owner: str,
        parent_fencing_token: int,
        expected_parent_revision: int,
        expected_child_parent_fencing_token: int,
        expected_child_revision: int,
        m3_job_id: str,
        approval_id: str,
        expected_m3_binding: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Rebind one approval-held routine publication child after recovery.

        This is deliberately narrower than a generic child reparent operation:
        the deterministic routine child and its exact M3 job must still be in
        the no-dispatch approval phase. The child relation and both matching
        checkpoints are updated with CAS while the recovered parent lease is
        current. A crashed caller can safely repeat the operation because the
        newly written ``prepared`` checkpoint carries the same M3 identity.
        """

        parent_job_id = _text(parent_job_id)
        child_job_id = _text(child_job_id)
        m3_job_id = _text(m3_job_id)
        approval_id = _text(approval_id)
        parent_owner = _text(parent_owner)
        try:
            parent_fencing_token = int(parent_fencing_token)
            expected_child_parent_fencing_token = int(expected_child_parent_fencing_token)
            expected_parent_revision = int(expected_parent_revision)
            expected_child_revision = int(expected_child_revision)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DurableJobLeaseError("routine publication adoption binding is malformed") from exc
        if (
            not parent_job_id
            or not child_job_id
            or not m3_job_id
            or not approval_id
            or not parent_owner
            or parent_fencing_token <= 0
            or expected_child_parent_fencing_token <= 0
            or not isinstance(expected_m3_binding, Mapping)
        ):
            raise DurableJobLeaseError("routine publication adoption binding is incomplete")

        expected_binding = dict(expected_m3_binding)
        required_binding_fields = {
            "routine_id",
            "routine_revision",
            "routine_version",
            "package_digest",
            "parent_invocation_job_id",
            "publication_child_job_id",
            "invocation_uuid",
            "owner_principal_id",
            "owner_session_id",
            "goal_id",
            "goal_revision",
            "source_watch_id",
            "connection_id",
            "connection_revision",
            "repository",
            "action",
            "operation_uuid",
        }
        if set(expected_binding) != required_binding_fields:
            raise DurableJobLeaseError("routine publication adoption binding fields are invalid")
        if (
            expected_binding.get("parent_invocation_job_id") != parent_job_id
            or expected_binding.get("publication_child_job_id") != child_job_id
        ):
            raise DurableJobLeaseError("routine publication adoption identity is stale")

        async with self._session() as db:
            bind = db.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect_name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            now = _utc_now()
            parent = await self._fetch(db, parent_job_id)
            child = await self._fetch(db, child_job_id)
            m3 = await self._fetch(db, m3_job_id)
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(parent, "goal_id", None),
                goal_revision=getattr(parent, "goal_revision", None),
                owner_kind=_text(getattr(parent, "owner_kind", None)),
                owner_principal_id=getattr(parent, "owner_principal_id", None),
                session_id=getattr(parent, "session_id", None),
                authority=getattr(parent, "declared_authority_json", None),
            )
            if (
                parent.status != "running"
                or _revision(parent) != expected_parent_revision
                or int(parent.fencing_token or 0) != parent_fencing_token
                or _text(parent.lease_owner) != parent_owner
                or not parent.lease_expires_at
                or _as_utc(parent.lease_expires_at) <= now
                or _deadline_expired(parent, now=now)
            ):
                raise DurableJobLeaseError("recovered routine parent lease changed")
            self._assert_lease(parent, owner=parent_owner, fencing_token=parent_fencing_token)

            parent_authority = _json_load(parent.declared_authority_json, {})
            child_authority = _json_load(child.declared_authority_json, {})
            m3_authority = _json_load(m3.declared_authority_json, {})
            m3_inputs = _json_load(m3.arguments_json, {})
            if not all(
                isinstance(value, Mapping)
                for value in (parent_authority, child_authority, m3_authority, m3_inputs)
            ):
                raise DurableJobTransitionError("routine publication authority is malformed")
            if (
                parent.job_kind != "routine_invocation"
                or parent.owner_kind != "user"
                or child.job_kind != "routine_github_followthrough_child"
                or child.owner_kind != "user"
                or child.run_identity != child_job_id
                or child.parent_job_id != parent_job_id
                or int(child.parent_fencing_token or 0)
                != expected_child_parent_fencing_token
                or _revision(child) != expected_child_revision
                or child.owner_principal_id != parent.owner_principal_id
                or child.session_id != parent.session_id
                or child.goal_id != parent.goal_id
                or child.goal_revision != parent.goal_revision
                or child_authority.get("parent_job_id") != parent_job_id
                or int(child_authority.get("parent_fencing_token") or 0)
                != expected_child_parent_fencing_token
                or child_authority.get("routine_invocation_job_id") != parent_job_id
                or child_authority.get("step_id") != "github_followthrough"
                or child_authority.get("m3_job_id") != m3_job_id
                or child_authority.get("publication_operation_uuid")
                != expected_binding.get("operation_uuid")
            ):
                raise DurableJobLeaseError("routine publication child binding changed")

            if child.status == "blocked":
                if child.lease_owner is not None or child.lease_expires_at is not None:
                    raise DurableJobLeaseError("blocked publication child still has a lease")
            elif child.status == "running":
                child_expiry = _as_utc(child.lease_expires_at)
                if (
                    not child.lease_owner
                    or child_expiry is None
                    or child_expiry > now
                ):
                    raise DurableJobLeaseError("publication child still has a live lease")
            else:
                raise DurableJobTransitionError("publication child is not recoverably approval-held")

            child_effects = _effect_ledger_or_raise(child.effect_receipts_json)
            safe_approval_hold = False
            if len(child_effects) == 1:
                child_effect = child_effects[0]
                effect_details = (
                    child_effect.get("details")
                    if isinstance(child_effect, Mapping)
                    else None
                )
                safe_approval_hold = (
                    child_effect.get("effect_type") == "guardian_routine_child"
                    and child_effect.get("status") == "blocked"
                    and child_effect.get("target_path") == f"routine-child:{child_job_id}"
                    and isinstance(effect_details, Mapping)
                    and dict(effect_details)
                    == {
                        "step_id": "github_followthrough",
                        "reason": "awaiting_publication_approval",
                        "status": "awaiting_approval",
                        "approval_id": approval_id,
                    }
                )
            if _job_has_unsafe_effects(child_effects) or (child_effects and not safe_approval_hold):
                raise DurableJobTransitionError(
                    "publication child has effect history requiring reconciliation"
                )

            checkpoints = _json_load(child.checkpoint_receipts_json, [])
            if not isinstance(checkpoints, list):
                raise DurableJobTransitionError("publication child checkpoint history is malformed")
            adoption = next(
                (
                    item
                    for item in reversed(checkpoints)
                    if isinstance(item, Mapping)
                    and item.get("checkpoint_id")
                    in {"routine-child:adoption_pending", "routine-child:prepared"}
                ),
                None,
            )
            adoption_payload = adoption.get("payload") if isinstance(adoption, Mapping) else None
            if (
                not isinstance(adoption_payload, Mapping)
                or adoption_payload.get("m3_job_id") != m3_job_id
                or adoption_payload.get("publication_operation_uuid")
                != expected_binding.get("operation_uuid")
            ):
                raise DurableJobTransitionError("routine publication adoption checkpoint is missing")
            if adoption.get("checkpoint_id") == "routine-child:adoption_pending" and (
                adoption_payload.get("status") != "prepare_pending"
            ):
                raise DurableJobTransitionError("routine publication adoption checkpoint is invalid")

            parent_owner_id = _text(parent.owner_principal_id)
            owner_session_id = _text(parent.session_id)
            if (
                parent_authority.get("routine_id") != expected_binding.get("routine_id")
                or int(parent_authority.get("routine_revision") or 0)
                != expected_binding.get("routine_revision")
                or int(parent_authority.get("routine_version") or 0)
                != expected_binding.get("routine_version")
                or parent_authority.get("package_digest") != expected_binding.get("package_digest")
                or parent_authority.get("invocation_uuid") != expected_binding.get("invocation_uuid")
                or parent_authority.get("source_watch_id") != expected_binding.get("source_watch_id")
                or parent_authority.get("github_connection_id") != expected_binding.get("connection_id")
                or int(parent_authority.get("github_connection_revision") or 0)
                != expected_binding.get("connection_revision")
                or parent_authority.get("github_repository") != expected_binding.get("repository")
                or parent_authority.get("github_action") != expected_binding.get("action")
                or expected_binding.get("owner_principal_id") != parent_owner_id
                or expected_binding.get("owner_session_id") != owner_session_id
                or expected_binding.get("goal_id") != parent.goal_id
                or expected_binding.get("goal_revision") != parent.goal_revision
            ):
                raise DurableJobLeaseError("routine publication parent binding changed")
            if (
                child_authority.get("routine_id") != expected_binding.get("routine_id")
                or int(child_authority.get("routine_revision") or 0)
                != expected_binding.get("routine_revision")
                or int(child_authority.get("routine_version") or 0)
                != expected_binding.get("routine_version")
                or child_authority.get("package_digest") != expected_binding.get("package_digest")
                or child_authority.get("invocation_uuid") != expected_binding.get("invocation_uuid")
            ):
                raise DurableJobLeaseError("routine publication child authority changed")

            m3_effects = _effect_ledger_or_raise(m3.effect_receipts_json)
            m3_checkpoints = _json_load(m3.checkpoint_receipts_json, [])
            m3_binding = m3_authority.get("routine_binding")
            if (
                m3.job_kind != "github_followthrough_v1"
                or m3.owner_kind != "user"
                or m3.owner_principal_id != parent_owner_id
                or m3.session_id != owner_session_id
                or m3.goal_id != parent.goal_id
                or m3.goal_revision != parent.goal_revision
                or m3.status not in {"awaiting_approval", "queued"}
                or not isinstance(m3_binding, Mapping)
                or dict(m3_binding) != expected_binding
                or m3_inputs.get("routine_binding") != expected_binding
                or m3_authority.get("approval_id") != approval_id
                or not m3.input_digest
                or _digest(m3_authority) != m3.authority_digest
                or _job_has_unsafe_effects(m3_effects)
                or any(item.get("effect_type") == "github_publication" for item in m3_effects)
                or not isinstance(m3_checkpoints, list)
                or any(
                    isinstance(item, Mapping)
                    and item.get("checkpoint_id") == "github-followthrough:dispatch_guard"
                    for item in m3_checkpoints
                )
            ):
                raise DurableJobTransitionError("routine publication M3 is not safely approval-held")

            approval = (
                await db.execute(
                    select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
                )
            ).scalars().first()
            allowed_approval_statuses = (
                {"pending", "approved"}
                if m3.status == "awaiting_approval"
                else {"approved", "consumed"}
            )
            approval_expiry = _as_utc(approval.expires_at) if approval is not None else None
            if (
                approval is None
                or approval.status not in allowed_approval_statuses
                or approval.owner_principal_id != parent_owner_id
                or _text(approval.operator_session_id or approval.session_id) != owner_session_id
                or approval_expiry is None
                or approval_expiry <= now
            ):
                raise DurableJobTransitionError("routine publication approval is no longer current")

            current_parent_fence = int(parent.fencing_token or 0)
            original_child_authority = dict(child_authority)
            rebound_child_authority = {
                **original_child_authority,
                "parent_fencing_token": current_parent_fence,
            }
            child_authority_json = _canonical(_safe_structure(rebound_child_authority))
            approval_context_json = child_authority_json
            child_payload = {
                "m3_job_id": m3_job_id,
                "approval_id": approval_id,
                "status": str(m3.status),
                "recovery": "adopted_child_checkpoint",
                "previous_parent_fencing_token": expected_child_parent_fencing_token,
                "parent_fencing_token": current_parent_fence,
            }
            parent_payload = {
                "step_id": "github_followthrough",
                "child_job_id": child_job_id,
                "m3_job_id": m3_job_id,
                "approval_id": approval_id,
                "status": str(m3.status),
                "recovery": "adopted_child_checkpoint",
                "parent_fencing_token": current_parent_fence,
            }

            def append_checkpoint(raw: str | None, checkpoint_id: str, state: Any, payload: Any, fence: int) -> str:
                history = _json_load(raw, [])
                if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
                    raise DurableJobTransitionError("durable checkpoint history is malformed")
                receipt = {
                    "checkpoint_id": checkpoint_id,
                    "state_digest": _digest(state),
                    "state_keys": sorted(str(key) for key in state.keys()) if isinstance(state, Mapping) else [],
                    "safe": True,
                    "recorded_at": now.isoformat(),
                    "fencing_token": fence,
                    "payload": _safe_structure(payload),
                }
                updated = [item for item in history if item.get("checkpoint_id") != checkpoint_id]
                updated.append(receipt)
                return _canonical(_bounded_checkpoint_receipts(updated))

            child_checkpoints_json = append_checkpoint(
                child.checkpoint_receipts_json,
                "routine-child:prepared",
                {"step_id": "github_followthrough", **child_payload},
                child_payload,
                int(child.fencing_token or 0),
            )
            parent_checkpoints_json = append_checkpoint(
                parent.checkpoint_receipts_json,
                "routine:publication_child_recorded",
                {"step_id": "github_followthrough", "child_job_id": child_job_id, "m3_job_id": m3_job_id},
                parent_payload,
                current_parent_fence,
            )

            parent_conditions = [
                WorkflowRunState.run_identity == parent_job_id,
                WorkflowRunState.status == "running",
                WorkflowRunState.revision == expected_parent_revision,
                WorkflowRunState.lease_owner == parent_owner,
                WorkflowRunState.lease_expires_at > now,
                WorkflowRunState.fencing_token == current_parent_fence,
            ]
            _append_goal_fence_condition(parent_conditions, parent)
            parent_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*parent_conditions)
                .values(
                    checkpoint_receipts_json=parent_checkpoints_json,
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(parent_update):
                raise DurableJobLeaseError("routine parent changed during publication adoption")

            child_conditions = [
                WorkflowRunState.run_identity == child_job_id,
                WorkflowRunState.status == child.status,
                WorkflowRunState.revision == expected_child_revision,
                WorkflowRunState.parent_job_id == parent_job_id,
                WorkflowRunState.parent_fencing_token == expected_child_parent_fencing_token,
                WorkflowRunState.owner_kind == "user",
                WorkflowRunState.owner_principal_id == parent_owner_id,
                WorkflowRunState.session_id == owner_session_id,
            ]
            _append_goal_fence_condition(child_conditions, child)
            if child.status == "blocked":
                child_conditions.extend(
                    (
                        WorkflowRunState.lease_owner.is_(None),
                        WorkflowRunState.lease_expires_at.is_(None),
                    )
                )
            else:
                child_conditions.extend(
                    (
                        WorkflowRunState.lease_owner == child.lease_owner,
                        WorkflowRunState.lease_expires_at == child.lease_expires_at,
                        WorkflowRunState.lease_expires_at <= now,
                    )
                )
            child_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*child_conditions)
                .values(
                    status="blocked",
                    failure_reason="awaiting_publication_approval",
                    lease_owner=None,
                    lease_expires_at=None,
                    parent_fencing_token=current_parent_fence,
                    declared_authority_json=child_authority_json,
                    approval_context_json=approval_context_json,
                    authority_digest=_digest(rebound_child_authority),
                    checkpoint_receipts_json=child_checkpoints_json,
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(child_update):
                raise DurableJobLeaseError("routine publication child changed during adoption")

            refreshed_parent = await self._fetch(db, parent_job_id)
            refreshed_child = await self._fetch(db, child_job_id)
            db.expunge(refreshed_parent)
            db.expunge(refreshed_child)
            return _serialize(refreshed_parent), _serialize(refreshed_child)

    async def record_recovery_checkpoint(
        self,
        job_id: str,
        *,
        owner_kind: str,
        owner_principal_id: str,
        checkpoint_id: str,
        state: Any,
        checkpoint_payload: Any | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append a checkpoint while an external effect is under recovery.

        Recovery runs deliberately have no worker lease.  This narrow seam is
        only for an authenticated owner recording evidence after an
        independent external readback; it cannot claim, dispatch, or resume a
        job.
        """
        if not _text(checkpoint_id):
            raise ValueError("checkpoint_id is required")
        if not _text(owner_kind) or not _text(owner_principal_id):
            raise DurableJobLeaseError("recovery owner identity is required")
        async with self._session() as db:
            # Recovery checkpoints also perform a read-modify-write of the
            # bounded checkpoint history.  Serialize that transaction on
            # SQLite before the fail-closed reservation validator runs.
            bind = db.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", "") == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            run = await self._fetch(db, job_id)
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
            if (
                _text(getattr(run, "owner_kind", None)) != _text(owner_kind)
                or _text(getattr(run, "owner_principal_id", None)) != _text(owner_principal_id)
            ):
                raise DurableJobLeaseError("recovery owner does not match durable job owner")
            if run.status not in {"unknown_external_effect", "cost_liability", "blocked", "failed"}:
                raise DurableJobTransitionError(
                    f"recovery checkpoint not allowed from {run.status}"
                )
            if run.lease_owner or run.lease_expires_at:
                raise DurableJobLeaseError("recovery checkpoint requires an unleased job")
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            receipt = {
                "checkpoint_id": checkpoint_id,
                "state_digest": _digest(state),
                "state_keys": sorted(str(key) for key in state.keys()) if isinstance(state, dict) else [],
                "safe": True,
                "payload": _safe_structure(checkpoint_payload) if checkpoint_payload is not None else None,
                "recorded_at": _utc_now().isoformat(),
                "recovery_owner_kind": owner_kind,
                "recovery_owner_principal_id": owner_principal_id,
            }
            # The same fail-closed rule applies to owner-bound recovery
            # checkpoints.  Non-repair rows retain their legacy tolerant
            # history handling below.
            if self._repo_repair_claimed(run):
                self._repo_repair_reservation_state(run)
            existing = _json_load(run.checkpoint_receipts_json, [])
            existing = [
                item
                for item in existing
                if isinstance(item, dict) and item.get("checkpoint_id") != checkpoint_id
            ]
            existing.append(receipt)
            now = _utc_now()
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.lease_owner.is_(None),
                WorkflowRunState.lease_expires_at.is_(None),
            ]
            _append_parent_fence_condition(conditions, run, now=now)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts(existing)),
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("job changed during recovery checkpoint")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "recovery_checkpoint", "status": "recorded", **receipt})

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

    async def record_recovery_artifact(
        self,
        job_id: str,
        *,
        owner_kind: str,
        owner_principal_id: str,
        file_path: str,
        artifact_type: str = "workspace_file",
        content: str | bytes | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist an artifact after recovery readback, without a worker lease."""
        if not _text(owner_kind) or not _text(owner_principal_id):
            raise DurableJobLeaseError("recovery owner identity is required")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
            if (
                _text(getattr(run, "owner_kind", None)) != _text(owner_kind)
                or _text(getattr(run, "owner_principal_id", None)) != _text(owner_principal_id)
            ):
                raise DurableJobLeaseError("recovery owner does not match durable job owner")
            if run.status not in {"unknown_external_effect", "cost_liability", "blocked", "failed"}:
                raise DurableJobTransitionError(f"recovery artifact not allowed from {run.status}")
            if run.lease_owner or run.lease_expires_at:
                raise DurableJobLeaseError("recovery artifact requires an unleased job")
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
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
                "recovery_owner_kind": owner_kind,
                "recovery_owner_principal_id": owner_principal_id,
            }
            existing = _json_load(run.artifact_receipts_json, [])
            existing = [
                item
                for item in existing
                if isinstance(item, dict) and item.get("artifact_id") != receipt["artifact_id"]
            ]
            existing.append(receipt)
            now = _utc_now()
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.lease_owner.is_(None),
                WorkflowRunState.lease_expires_at.is_(None),
            ]
            _append_parent_fence_condition(conditions, run, now=now)
            updated = await db.execute(
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
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("job changed during recovery artifact write")
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "recovery_artifact", "status": "recorded", **receipt})

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
        readback_id: str | None = None,
        verified_at: str | None = None,
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
        readback_id = _text(readback_id) or None
        verified_at = _text(verified_at) or None
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
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
            recovery_readback = (
                receipt_kind == "readback"
                and owner is None
                and fencing_token is None
                and isinstance(safe_details, Mapping)
                and _text(safe_details.get("reconciliation_owner_id"))
                == _text(getattr(run, "owner_principal_id", None))
                and run.status in {"unknown_external_effect", "cost_liability", "blocked", "failed"}
                and not run.lease_owner
                and not run.lease_expires_at
            )
            if _deadline_expired(run) and not recovery_readback:
                raise DurableJobTransitionError("job deadline has expired")
            if run.status in DURABLE_JOB_TERMINAL_STATUSES or (
                run.status == "failed" and not recovery_readback
            ):
                raise DurableJobTransitionError(f"terminal job cannot record effects ({run.status})")
            lease_present = bool(run.lease_owner or run.lease_expires_at)
            if run.status == "running" and (owner is None or fencing_token is None):
                raise DurableJobLeaseError("active jobs require owner and fencing token for effect writes")
            if lease_present or run.status == "running":
                if owner is None or fencing_token is None:
                    raise DurableJobLeaseError("owner and fencing token are required for leased effect writes")
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            elif owner is None and fencing_token is None:
                if recovery_readback:
                    pass
                elif not _is_typed_admission_receipt(
                    run,
                    effect_type=effect_type,
                    receipt_kind=receipt_kind,
                    status=status,
                    details=safe_details,
                    owner=owner,
                    fencing_token=fencing_token,
                ):
                    raise DurableJobLeaseError(
                        "accepted jobs require an authenticated owner lease for effect writes"
                    )
            else:
                self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            recorded_at = _utc_now().isoformat()
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
                "recorded_at": recorded_at,
                "fencing_token": fencing_token,
            }
            if receipt_kind == "readback":
                # Readback identity and verification time must come from the
                # capability's actual verifier.  The durable runtime stores
                # them when supplied; it never promotes an operation ID or its
                # insertion time into proof fields.
                if readback_id:
                    receipt["readback_id"] = readback_id
                if verified_at:
                    receipt["verified_at"] = verified_at
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
            if previous is not None:
                # A readback is an observation of the original effect.  Keep
                # its stable authority and adapter identity when the caller
                # only supplies the target and verification fields.
                for field_name in ("approval_id", "adapter_idempotency_key"):
                    if not receipt.get(field_name):
                        receipt[field_name] = previous.get(field_name)
                if receipt_kind == "readback":
                    for field_name in ("readback_id", "verified_at"):
                        if not receipt.get(field_name):
                            receipt[field_name] = previous.get(field_name)
            previous_status = _text(previous.get("status")) if previous is not None else ""
            remote_terminal_settlement = (
                effect_type == "remote_inference_admission"
                and status in {"succeeded", "failed"}
                and isinstance(safe_details, dict)
                and isinstance(safe_details.get("receipt"), dict)
                and _text(safe_details["receipt"].get("status"))
                in {"succeeded", "settled", "failed", "cancelled", "expired", "rejected"}
            )
            remote_uncertain_projection = (
                effect_type == "remote_inference_admission"
                and status == "blocked"
                and isinstance(safe_details, dict)
                and isinstance(safe_details.get("receipt"), dict)
                and _text(safe_details["receipt"].get("status")) == "blocked"
            )
            if remote_terminal_settlement:
                prior_details = previous.get("details") if previous is not None else {}
                prior_details = prior_details if isinstance(prior_details, Mapping) else {}
                nested_receipt = safe_details.get("receipt") if isinstance(safe_details, Mapping) else None
                nested_receipt = nested_receipt if isinstance(nested_receipt, Mapping) else {}
                expected_operation_id = _text(
                    prior_details.get("operation_id")
                ) or _text(previous.get("target_digest") if previous is not None else None) or _text(
                    target_digest
                )
                expected_job_id = _text(prior_details.get("job_id")) or job_id
                expected_owner_id = _text(prior_details.get("owner_id")) or _text(
                    getattr(run, "owner_principal_id", None)
                )
                if (
                    not expected_operation_id
                    or not expected_job_id
                    or not expected_owner_id
                    or _text(nested_receipt.get("operation_id")) != expected_operation_id
                    or _text(nested_receipt.get("job_id")) != expected_job_id
                    or _text(nested_receipt.get("owner_id")) != expected_owner_id
                ):
                    raise DurableJobIdempotencyConflict(
                        "remote terminal receipt does not match the immutable intent binding"
                    )
            if previous_status in UNRESOLVED_EFFECT_STATUSES:
                # A terminal broker receipt is the exact settlement of the
                # pre-dispatch remote intent.  Provider errors remain
                # ``blocked`` and therefore still require reconciliation;
                # only the broker's settled success/failure states may close
                # this one effect identity.
                if (
                    receipt_kind == "effect"
                    and status not in UNRESOLVED_EFFECT_STATUSES
                    and not remote_terminal_settlement
                    and not remote_uncertain_projection
                ):
                    raise DurableJobTransitionError(
                        "unresolved external effect requires exact readback or cost settlement"
                    )
                if (
                    receipt_kind == "effect"
                    and not remote_terminal_settlement
                    and not remote_uncertain_projection
                ):
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
                if remote_terminal_settlement:
                    prior_details = previous.get("details")
                    if isinstance(prior_details, Mapping) and isinstance(safe_details, dict):
                        # Keep the immutable route/profile selected at intent
                        # time attached to the terminal settlement. A restart
                        # must be able to audit the exact route that crossed
                        # the provider boundary.
                        safe_details = {**dict(prior_details), **safe_details}
                        receipt["details"] = safe_details
            if receipt_kind == "readback" and status == "succeeded":
                if not _verified_readback_exists([receipt]):
                    raise DurableJobTransitionError(
                        "successful readback requires verified capability evidence"
                    )
                receipt["reconciled"] = True
                receipt["reconciliation_status"] = "resolved"
                existing = _resolve_readback_observations(existing, receipt)
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
        readback_id: str | None = None,
        verified_at: str | None = None,
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
            readback_id=readback_id,
            verified_at=verified_at,
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

        A durable job row is required; this method never creates one. Every
        effect receipt must provide the existing owner/fencing pair. The only
        ownerless write permitted on an ``accepted`` row is the narrow typed
        authority-denial projection; a remote admission receipt must wait
        until its durable job has been claimed.
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
        from src.db.models import InferenceCostReservation
        async with self._session() as accounting_db:
            accounting_row = await accounting_db.get(InferenceCostReservation, str(safe_receipt["operation_id"]))
            unknown_cost = accounting_row is not None and accounting_row.state in {"contact_started", "unknown"}
        return await self.record_effect(
            job_id,
            effect_type="remote_inference_admission",
            effect_id=f"remote_inference:{safe_receipt['operation_id']}",
            target_path=f"remote_inference:{safe_receipt['operation_id']}",
            target_digest=_text(safe_receipt.get("operation_id")) or None,
            adapter_idempotency_key=_text(safe_receipt.get("operation_id")) or None,
            status="unknown" if unknown_cost else REMOTE_INFERENCE_EFFECT_STATUSES[admission_status],
            details={
                "admission_status": admission_status,
                "receipt": safe_receipt,
                "receipt_digest": receipt_digest,
                "unknown_cost_outstanding": unknown_cost,
            },
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
        )

    async def record_remote_inference_intent(
        self,
        *,
        operation_id: str,
        job_id: str,
        owner_id: str,
        parent_job_id: str | None = None,
        runtime_path: str = "",
        profile_id: str = "",
        priority: str = "",
        deadline_at: float | None = None,
        capability_version: str = "",
        owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        """Fence one remote operation before its provider callback starts.

        The process-local remote broker cannot survive a worker restart.  The
        intent effect therefore reserves the durable operation identity under
        the existing job lease before dispatch.  A repeated operation ID is a
        hard idempotency conflict, including after a process restart; callers
        must reconcile the prior effect instead of issuing another provider
        request.
        """
        operation_id = _bounded_identifier(operation_id, field_name="operation_id", limit=256)
        job_id = _bounded_identifier(job_id, field_name="job_id", limit=256)
        owner_id = _bounded_identifier(owner_id, field_name="owner_id", limit=256)
        if not operation_id or not job_id or not owner_id:
            raise DurableJobLeaseError("remote inference intent requires operation, job, and owner identities")
        parent_job_id = (
            _bounded_identifier(parent_job_id, field_name="parent_job_id", limit=256)
            if parent_job_id is not None
            else None
        )
        runtime_path = _bounded_identifier(runtime_path, field_name="runtime_path", limit=128)
        profile_id = _bounded_identifier(profile_id, field_name="profile_id", limit=256)
        priority = _bounded_identifier(priority, field_name="priority", limit=64)
        capability_version = _bounded_identifier(
            capability_version,
            field_name="capability_version",
            limit=256,
        )
        normalized_deadline = None if deadline_at is None else float(deadline_at)
        if normalized_deadline is not None and not math.isfinite(normalized_deadline):
            raise DurableJobTransitionError("remote inference deadline is malformed")
        effect_id = f"remote_inference:{operation_id}"
        target_path = f"remote_inference:{operation_id}"
        intent_details = {
            "admission_status": "intent",
            "operation_id": operation_id,
            "job_id": job_id,
            "owner_id": owner_id,
            "parent_job_id": parent_job_id,
            "runtime_path": runtime_path,
            "profile_id": profile_id,
            "priority": priority,
            "deadline_at": normalized_deadline,
            "capability_version": capability_version,
        }
        safe_details = _safe_structure(intent_details)
        async with self._session() as db:
            now = _utc_now()
            run = await self._fetch(db, job_id)
            if _deadline_expired(run, now=now):
                raise DurableJobTransitionError("job deadline has expired")
            if run.status in DURABLE_JOB_TERMINAL_STATUSES:
                raise DurableJobTransitionError("terminal durable job cannot dispatch remote inference")
            persisted_owner = _text(getattr(run, "owner_principal_id", None))
            if persisted_owner != owner_id:
                raise DurableJobLeaseError(
                    "remote inference intent owner does not match the durable job owner"
                )
            if owner is None or fencing_token is None:
                raise DurableJobLeaseError(
                    "remote inference intent requires the active durable job lease"
                )
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            effects = _effect_ledger_or_raise(run.effect_receipts_json)
            previous = next(
                (
                    item
                    for item in effects
                    if isinstance(item, dict) and _text(item.get("effect_id")) == effect_id
                ),
                None,
            )
            if previous is not None:
                previous_details = previous.get("details")
                previous_details = previous_details if isinstance(previous_details, Mapping) else {}
                route_fields = ("runtime_path", "profile_id")
                if any(
                    _text(previous_details.get(field_name)) != _text(intent_details[field_name])
                    for field_name in route_fields
                ):
                    raise DurableJobIdempotencyConflict(
                        "remote inference operation identity is bound to a different route/profile"
                    )
                if previous.get("status") == "blocked" and previous_details.get("never_contacted") is True and await self._accounting_resume_claim_allowed(db, run):
                    db.expunge(run)
                    return _serialize(run, receipt=previous)
                raise DurableJobIdempotencyConflict(
                    "remote inference operation identity is already fenced"
                )
            receipt = {
                "effect_id": effect_id,
                "receipt_kind": "effect",
                "effect_type": "remote_inference_admission",
                "target_path": target_path,
                "target_digest": operation_id,
                "approval_id": None,
                "adapter_idempotency_key": operation_id,
                "status": "intent",
                "content_sha256": None,
                "details": safe_details,
                "recorded_at": now.isoformat(),
                "fencing_token": fencing_token,
            }
            current_revision = _revision(run)
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.lease_owner == owner,
                WorkflowRunState.fencing_token == fencing_token,
                WorkflowRunState.lease_expires_at > now,
            ]
            _append_parent_fence_condition(conditions, run, now=now)
            effects.append(receipt)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    effect_receipts_json=_canonical(_bounded_effect_ledger(effects)),
                    updated_at=now,
                    heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1,
                )
            )
            if not _rowcount_is_one(updated):
                # The revision predicate is the durable unique-operation CAS.
                # A concurrent process that won the same identity cannot be
                # overwritten by this stale intent.
                raise DurableJobLeaseError(
                    "remote inference intent lost the concurrent durable fence"
                )
            refreshed = await self._fetch(db, job_id)
            db.expunge(refreshed)
            return _serialize(
                refreshed,
                receipt={"kind": "effect", "status": "recorded", **receipt},
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
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
            if run.status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES:
                raise DurableJobTransitionError(
                    f"{run.status} requires explicit reconciliation before retry"
                )
            if run.status != "failed":
                raise DurableJobTransitionError(f"only failed jobs may be retried (current={run.status})")
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            if _cleanup_reservation_pending(run):
                raise DurableJobTransitionError(
                    "private artifact cleanup is reserved; reconcile the exact cleanup receipt before retry"
                )
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
                if not _effect_is_unresolved(matched_effect):
                    if _text(matched_effect.get("status")) == "succeeded":
                        raise DurableJobTransitionError(
                            "retry reconciliation cannot reuse an already-succeeded effect"
                        )
                    if not _retry_reconciliation_marker_matches(
                        existing_effects,
                        matched_effect,
                        receipt_payload,
                        receipt_digest,
                    ):
                        raise DurableJobTransitionError(
                            "retry reconciliation requires the exact receipt that resolved an unresolved effect"
                        )
                else:
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
            retry_conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == "failed",
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.owner_kind == owner_kind,
                WorkflowRunState.owner_principal_id == owner_principal_id,
                WorkflowRunState.service_id == service_id,
            ]
            _append_goal_fence_condition(retry_conditions, run)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*retry_conditions)
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

    async def finalize_reconciled_job(
        self,
        job_id: str,
        *,
        owner_kind: str,
        owner_principal_id: str,
        expected_revision: int | None = None,
        result: Any = None,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        """Close an uncertain job only after its exact effect was read back.

        This is intentionally separate from ``transition_job``: recovery has
        no worker lease and must never make an arbitrary blocked job
        successful.  The effect ledger must already contain a verified
        readback and no unresolved liability.
        """
        if not _text(owner_kind) or not _text(owner_principal_id):
            raise DurableJobLeaseError("recovery owner identity is required")
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
            if (
                _text(getattr(run, "owner_kind", None)) != _text(owner_kind)
                or _text(getattr(run, "owner_principal_id", None)) != _text(owner_principal_id)
            ):
                raise DurableJobLeaseError("recovery owner does not match durable job owner")
            if run.status not in {"unknown_external_effect", "cost_liability", "blocked", "failed"}:
                raise DurableJobTransitionError(
                    f"only an uncertain job may be finalized by reconciliation (current={run.status})"
                )
            if run.lease_owner or run.lease_expires_at:
                raise DurableJobLeaseError("reconciled finalization requires an unleased job")
            current_revision = _revision(run)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise DurableJobLeaseError("durable job revision is stale")
            effects = _effect_ledger_or_raise(run.effect_receipts_json)
            if _job_has_unsafe_effects(effects):
                raise DurableJobTransitionError(
                    "cannot finalize reconciliation while an external effect remains unresolved"
                )
            if not _verified_readback_exists(effects):
                raise DurableJobTransitionError(
                    "reconciled finalization requires a verified capability readback"
                )
            now = _utc_now()
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.owner_kind == owner_kind,
                WorkflowRunState.owner_principal_id == owner_principal_id,
                WorkflowRunState.lease_owner.is_(None),
                WorkflowRunState.lease_expires_at.is_(None),
            ]
            _append_parent_fence_condition(conditions, run, now=now)
            values: dict[str, Any] = {
                "status": "succeeded",
                "failure_reason": None,
                "lease_owner": None,
                "lease_expires_at": None,
                "finished_at": now,
                "updated_at": now,
                "heartbeat_at": now,
                "revision": WorkflowRunState.revision + 1,
            }
            if result is not None:
                values["result_digest"] = _digest(result)
                values["result_summary"] = _text(result_summary, "reconciled result recorded")
            elif result_summary is not None:
                values["result_summary"] = _text(result_summary)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(**values)
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("job changed during reconciled finalization")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "reconciled_finalization",
                "status": "recorded",
                "owner_kind": owner_kind,
                "owner_principal_id": owner_principal_id,
                "reason": "verified_external_readback",
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
            await _assert_canonical_goal_fence(
                db,
                goal_id=getattr(run, "goal_id", None),
                goal_revision=getattr(run, "goal_revision", None),
                owner_kind=_text(getattr(run, "owner_kind", None)),
                owner_principal_id=getattr(run, "owner_principal_id", None),
                session_id=getattr(run, "session_id", None),
                authority=getattr(run, "declared_authority_json", None),
            )
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
                    _reconciliation_matches_effect(item, receipt_payload)
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
            reconciliation_conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == run.status,
                WorkflowRunState.revision == current_revision,
                WorkflowRunState.owner_kind == owner_kind,
                WorkflowRunState.owner_principal_id == owner_principal_id,
                WorkflowRunState.service_id == service_id,
            ]
            _append_goal_fence_condition(reconciliation_conditions, run)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*reconciliation_conditions)
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
        recovered: list[dict[str, Any]] = await self.recover_inference_accounting(now=observed_at)
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
                recovery_effects: list[dict[str, Any]] | None = None
                try:
                    await _assert_canonical_goal_fence(
                        db,
                        goal_id=getattr(run, "goal_id", None),
                        goal_revision=getattr(run, "goal_revision", None),
                        owner_kind=_text(getattr(run, "owner_kind", None)),
                        owner_principal_id=getattr(run, "owner_principal_id", None),
                        session_id=getattr(run, "session_id", None),
                        authority=getattr(run, "declared_authority_json", None),
                    )
                except DurableJobTransitionError as exc:
                    if str(exc) != "durable job goal revision is stale":
                        # A missing, unbound, or revoked goal is not recoverable
                        # by a job-only transition. Leave it for the canonical
                        # owner/deletion path rather than clearing authority.
                        continue

                    # The canonical goal still exists but has advanced beyond
                    # this run. Clear the expired lease through a dedicated
                    # old-identity/current-later-revision CAS. No effect or
                    # delivery receipt is synthesized on this path.
                    recovery_conditions = [
                        WorkflowRunState.run_identity == run.run_identity,
                        WorkflowRunState.status == "running",
                        WorkflowRunState.revision == expected_revision,
                        WorkflowRunState.fencing_token == expected_token,
                    ]
                    _append_stale_goal_revision_condition(
                        recovery_conditions,
                        run,
                        observed_at=observed_at,
                    )
                    updated = await db.execute(
                        update(WorkflowRunState)
                        .execution_options(synchronize_session=False)
                        .where(*recovery_conditions)
                        .values(
                            status="blocked",
                            failure_reason="stale_goal_revision",
                            lease_owner=None,
                            lease_expires_at=None,
                            updated_at=observed_at,
                            heartbeat_at=observed_at,
                            revision=WorkflowRunState.revision + 1,
                            fencing_token=WorkflowRunState.fencing_token + 1,
                        )
                    )
                    if not _rowcount_is_one(updated):
                        continue
                    refreshed = await self._fetch(db, run.run_identity)
                    db.expunge(refreshed)
                    recovered.append(
                        _serialize(
                            refreshed,
                            receipt={
                                "kind": "restart_recovery",
                                "status": "blocked",
                                "reason": "stale_goal_revision",
                                "recovery_state": "stale_goal_revision",
                                "previous_owner": old_owner,
                                "fencing_token": refreshed.fencing_token,
                                "revision": _revision(refreshed),
                                "operator_action": (
                                    "discard_stale_goal_revision_before_new_admission"
                                ),
                                "stale_goal_revision": True,
                                "operator_visible": True,
                            },
                        )
                    )
                    continue
                try:
                    persisted_deadline = _as_utc(run.deadline_at)
                except ValueError:
                    persisted_deadline = None
                    recovered_status, recovery_reason = (
                        "blocked",
                        "stale_lease_malformed_deadline_requires_reconciliation",
                    )
                else:
                    if persisted_deadline is not None and persisted_deadline <= observed_at:
                        recovered_status, recovery_reason = "failed", "deadline_expired"
                        recovery_effects = None
                    else:
                        try:
                            effects = _effect_ledger_or_raise(run.effect_receipts_json)
                        except DurableJobTransitionError:
                            effects = None
                        terminal_settlement = (
                            _terminal_remote_settlement_effect(
                                effects,
                                expected_job_id=run.run_identity,
                                expected_owner_id=_text(run.owner_principal_id),
                            )
                            if effects is not None and not _job_has_unsafe_effects(effects)
                            else None
                        )
                        if terminal_settlement is not None:
                            recovered_status = "succeeded"
                            recovery_reason = "remote_terminal_settlement_recovered"
                            recovery_effects = [
                                *effects,
                                _remote_terminal_recovery_readback(
                                    terminal_settlement,
                                    observed_at=observed_at,
                                ),
                            ]
                        else:
                            recovered_status, recovery_reason = _restart_recovery_state(run)
                            recovery_effects = None
                recovery_values: dict[str, Any] = {
                    "status": recovered_status,
                    "failure_reason": recovery_reason,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "fencing_token": WorkflowRunState.fencing_token + 1,
                    "revision": WorkflowRunState.revision + 1,
                    "updated_at": observed_at,
                    "heartbeat_at": observed_at,
                    "finished_at": (
                        observed_at
                        if recovered_status in {"failed", "succeeded"}
                        else None
                    ),
                }
                if recovery_effects is not None:
                    recovery_values["effect_receipts_json"] = _canonical(
                        _bounded_effect_ledger(recovery_effects)
                    )
                recovery_conditions = [
                    WorkflowRunState.run_identity == run.run_identity,
                    WorkflowRunState.status == "running",
                    WorkflowRunState.revision == expected_revision,
                    WorkflowRunState.fencing_token == expected_token,
                ]
                _append_goal_fence_condition(recovery_conditions, run)
                updated = await db.execute(
                    update(WorkflowRunState)
                    .execution_options(synchronize_session=False)
                    .where(*recovery_conditions)
                    .values(**recovery_values)
                )
                if not _rowcount_is_one(updated):
                    continue
                refreshed = await self._fetch(db, run.run_identity)
                receipt = {
                    "kind": "restart_recovery",
                    "status": recovered_status,
                    "reason": recovery_reason,
                    "recovery_state": recovered_status,
                    "previous_owner": old_owner,
                    "fencing_token": refreshed.fencing_token,
                    "revision": _revision(refreshed),
                    "operator_action": (
                        "deadline_expired_no_retry"
                        if recovered_status == "failed" and recovery_reason == "deadline_expired"
                        else (
                            "resume_already_settled_remote_operation"
                            if recovered_status == "succeeded"
                            else (
                                "reconcile_external_effect_and_cost_then_retry_or_cancel"
                                if recovered_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES
                                else "reconcile_effects_then_retry_or_cancel"
                            )
                        )
                    ),
                    "operator_visible": True,
                }
                db.expunge(refreshed)
                recovered.append(_serialize(refreshed, receipt=receipt))
        return recovered

    async def recover_stale_job(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Recover one expired lease without touching unrelated jobs.

        Capability-specific recovery routes must not run the global stale-job
        sweep as a side effect of inspecting one receipt.  This narrow CAS
        uses the same conservative classification as ``recover_stale_jobs``:
        an expired worker is cleared to a visible blocked/failed state, while
        any unresolved effect remains authoritative.  A caller can then make
        a capability-specific decision under the new fencing token.
        """

        observed_at = now or _utc_now()
        await self.recover_inference_accounting(now=observed_at, job_id=job_id)
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status != "running":
                db.expunge(run)
                return _serialize(run, receipt={"kind": "targeted_recovery", "status": "noop", "operator_visible": True})
            try:
                lease_expires = _as_utc(run.lease_expires_at)
            except ValueError as exc:
                raise DurableJobTransitionError("job lease metadata is malformed") from exc
            if lease_expires is not None and lease_expires > observed_at:
                raise DurableJobLeaseError("job lease is still active")
            try:
                deadline = _as_utc(run.deadline_at)
            except ValueError:
                deadline = None
            if deadline is not None and deadline <= observed_at:
                recovery_status, recovery_reason = "failed", "deadline_expired"
            else:
                recovery_status, recovery_reason = _restart_recovery_state(run)
            expected_revision = _revision(run)
            expected_fence = int(run.fencing_token or 0)
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == "running",
                WorkflowRunState.revision == expected_revision,
                WorkflowRunState.fencing_token == expected_fence,
            ]
            _append_goal_fence_condition(conditions, run)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    status=recovery_status,
                    failure_reason=recovery_reason,
                    lease_owner=None,
                    lease_expires_at=None,
                    fencing_token=WorkflowRunState.fencing_token + 1,
                    revision=WorkflowRunState.revision + 1,
                    updated_at=observed_at,
                    heartbeat_at=observed_at,
                    finished_at=(observed_at if recovery_status == "failed" else None),
                )
            )
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("job changed during targeted recovery")
            refreshed = await self._fetch(db, job_id)
            receipt = {
                "kind": "targeted_recovery",
                "status": recovery_status,
                "reason": recovery_reason,
                "previous_owner": run.lease_owner,
                "fencing_token": refreshed.fencing_token,
                "revision": _revision(refreshed),
                "operator_visible": True,
            }
            db.expunge(refreshed)
            return _serialize(refreshed, receipt=receipt)

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
        # Resolve dynamically so DB fixtures and migration shims can patch the
        # canonical job runtime session factory without changing production
        # persistence behavior.
        return get_session()


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
    "get_session",
    "durable_job_repository",
]
