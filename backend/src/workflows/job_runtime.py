"""Bounded durable invocation contract backed by ``WorkflowRunState``.

The workflow state table is the canonical execution record.  This module adds
the typed admission/lifecycle operations needed by capabilities without
introducing another queue or state machine.  It deliberately records hashes
and structural metadata for inputs, checkpoints, and results; callers must
store sensitive values in their existing governed stores.
"""

from __future__ import annotations

from src.workflows.durable_state import _begin_legacy_aware_writer

import hashlib
import json
import math
import re
import asyncio
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from types import MappingProxyType
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import false, func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import select

from src.artifacts.registry import build_artifact_record
from src.db.models import ApprovalRequest, Goal, GuardianRoutine, GuardianRoutineVersion, WorkflowRunState
from src.db.session_refs import ensure_sessions_exist
from src.workflows.inference_accounting import InferenceAccountingRepositoryMixin
from src.runtime_plugins.ownership import RuntimeCompositionBinding


@dataclass(eq=False)
class _OriginalMemoryMaintenanceScope:
    repository: Any
    task: Any
    invocation: object
    header_budget: Any
    live: bool = True


_MEMORY_MAINTENANCE_SCOPES = {}
_CURRENT_MEMORY_MAINTENANCE = ContextVar("original_memory_maintenance", default=None)


async def _deny_original_memory_generic_body(repository, job_id):
    """A target scalar denies a generic writer; it issues no frame or Source."""
    if type(job_id) is not str:
        return
    async with repository._session() as db:
        rows = (await db.execute(text(
            "SELECT typeof(job_kind),job_kind COLLATE BINARY='runtime_service_memory_v1' "
            "FROM workflow_run_states INDEXED BY ix_workflow_run_states_run_identity "
            "WHERE run_identity COLLATE BINARY=:identity LIMIT 2"),
            {"identity": job_id})).all()
        if rows == [("text", 1)]:
            raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")


def _deny_original_memory_generic_entry(method):
    parameters = signature(method)
    @wraps(method)
    async def denied(self, *args, **kwargs):
        bound = parameters.bind(self, *args, **kwargs)
        await _deny_original_memory_generic_body(self, bound.arguments["job_id"])
        return await method(self, *args, **kwargs)
    return denied


def _original_memory_maintenance_scope(repository):
    scope = _CURRENT_MEMORY_MAINTENANCE.get()
    if scope is None:
        return None
    if (type(scope) is not _OriginalMemoryMaintenanceScope
            or _MEMORY_MAINTENANCE_SCOPES.get(id(scope)) is not scope
            or not scope.live or scope.repository is not repository
            or scope.task is not asyncio.current_task()):
        raise DurableJobLeaseError("original_memory_maintenance_scope_unavailable")
    return scope


def _original_memory_maintenance_entry(method):
    """One original call owns capacity across its existing nested transactions."""
    @wraps(method)
    async def scoped(self, *args, **kwargs):
        # Preserve the existing initial native queue's original numeric frame.
        # Its original admission/queue gates remain inside transition_job.
        if (method.__name__ == "transition_job" and kwargs.get("header_budget") is not None
                and (args[1] if len(args) > 1 else kwargs.get("to_status")) == "queued"):
            return await method(self, *args, **kwargs)
        if _original_memory_maintenance_scope(self) is not None:
            return await method(self, *args, **kwargs)
        budget = None
        async with self._session() as db:
            present = await db.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM workflow_run_states "
                "WHERE job_kind COLLATE BINARY='runtime_service_memory_v1')"))
            if present:
                from src.memory.universe import native_memory_universe
                from src.memory.header_bounds import HeaderReadBudget
                connection = await db.connection()
                if not (await connection.get_raw_connection()).driver_connection.in_transaction:
                    await db.execute(text("BEGIN"))
                await native_memory_universe(db)
                budget = HeaderReadBudget()
            await db.rollback()
        scope = _OriginalMemoryMaintenanceScope(self, asyncio.current_task(), object(), budget)
        _MEMORY_MAINTENANCE_SCOPES[id(scope)] = scope
        token = _CURRENT_MEMORY_MAINTENANCE.set(scope)
        try:
            from src.memory.composition_headers import _memory_current_snapshot_scope
            with (_memory_current_snapshot_scope(budget) if budget is not None else nullcontext()):
                return await method(self, *args, **kwargs)
        finally:
            scope.live = False
            _CURRENT_MEMORY_MAINTENANCE.reset(token)
            _MEMORY_MAINTENANCE_SCOPES.pop(id(scope), None)
    return scoped


async def _original_memory_maintenance_snapshot(repository, db, *, writer=False):
    scope = _original_memory_maintenance_scope(repository)
    if scope is None or scope.header_budget is None:
        if scope is not None and await db.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM workflow_run_states "
                "WHERE job_kind COLLATE BINARY='runtime_service_memory_v1')")):
            raise DurableJobLeaseError("original_memory_maintenance_route_changed")
        return None
    connection = await db.connection()
    if not (await connection.get_raw_connection()).driver_connection.in_transaction:
        await db.execute(text("BEGIN IMMEDIATE" if writer else "BEGIN"))
    from src.memory.universe import native_memory_universe
    from src.memory.composition_headers import _certify_current_memory_snapshot
    await native_memory_universe(db)
    return await _certify_current_memory_snapshot(db, scope.header_budget)


@dataclass(frozen=True, eq=False)
class _OriginalMemoryNegativeOwner:
    pending_changes: Mapping
    statement: Any
    outputs: tuple
    header_budget: Any
    before_values: Mapping
    tx: Any
    driver: Any
    total_changes: int
    _repository: Any = field(repr=False)
    _task: Any = field(repr=False)
    _scope: Any = field(repr=False)
    _db: Any = field(repr=False)
    _run: Any = field(repr=False)


_MEMORY_NEGATIVE_OWNERS = {}


async def _validate_original_memory_negative_owner(db, run, original_owner):
    from src.memory.header_bounds import _connection_state
    from src.workspace.accounting_witness import _native_memory_planned_sql_row
    from sqlalchemy import inspect
    owner = original_owner
    if (type(owner) is not _OriginalMemoryNegativeOwner
            or _MEMORY_NEGATIVE_OWNERS.get(id(owner)) is not owner
            or owner._task is not asyncio.current_task() or owner._db is not db
            or owner._run is not run or inspect(run).session is not db.sync_session
            or inspect(run).modified
            or _original_memory_maintenance_scope(owner._repository) is not owner._scope
            or owner._scope.header_budget is not owner.header_budget
            or _native_memory_planned_sql_row(run, {}) != dict(owner.before_values)):
        raise DurableJobLeaseError("original_memory_negative_owner_unavailable")
    tx, driver, changes = await _connection_state(db)
    if tx is not owner.tx or driver is not owner.driver or changes != owner.total_changes:
        raise DurableJobLeaseError("original_memory_negative_writer_changed")
    return owner


async def _execute_original_memory_negative(repository, db, run, conditions, pending_changes, *, receipt):
    """Only the original gated negative branches call this private issuer."""
    if run.job_kind != "runtime_service_memory_v1":
        return await db.execute(update(WorkflowRunState).execution_options(
            synchronize_session=False).where(*conditions).values(**pending_changes))
    scope = _original_memory_maintenance_scope(repository)
    if scope is None or scope.header_budget is None:
        raise DurableJobLeaseError("original_memory_negative_scope_unavailable")
    if pending_changes.get("status") not in {"failed", "cancelled", "blocked", "unknown_external_effect", "cost_liability"}:
        raise DurableJobTransitionError("original_memory_negative_outcome_required")
    forbidden = {"effect_receipts_json", "checkpoint_receipts_json", "checkpoint_context_json",
        "artifact_receipts_json", "arguments_json", "declared_authority_json", "composition_binding_json"}
    if forbidden.intersection(pending_changes):
        raise DurableJobTransitionError("original_memory_negative_patch_unsupported")
    changes = dict(pending_changes)
    for column in ("revision", "fencing_token"):
        if column in changes and type(changes[column]) is not int:
            changes[column] = int(getattr(run, column) or 0) + 1
    from src.memory.header_bounds import _connection_state
    from src.workspace.accounting_witness import (_native_memory_planned_sql_row,
        prepare_native_memory_unknown, apply_native_memory_unknown)
    tx, driver, total_changes = await _connection_state(db)
    statement = update(WorkflowRunState).execution_options(synchronize_session=False).where(
        *conditions).values(**changes)
    owner = _OriginalMemoryNegativeOwner(MappingProxyType(changes), statement,
        (_native_memory_pending_output(run, changes, receipt=receipt),), scope.header_budget,
        MappingProxyType(_native_memory_planned_sql_row(run, {})), tx, driver, total_changes,
        repository, asyncio.current_task(), scope, db, run)
    _MEMORY_NEGATIVE_OWNERS[id(owner)] = owner
    try:
        plan = await prepare_native_memory_unknown(db, run, original_owner=owner)
        return await apply_native_memory_unknown(db, run, plan)
    finally:
        _MEMORY_NEGATIVE_OWNERS.pop(id(owner), None)


@dataclass(frozen=True)
class NodeProcessCleanupSettlement:
    """Internal original authority request, never a caller-supplied proof."""
    job_id: str
    expected_revision: int
    owner_principal_id: str
    owner_session_id: str
    authority: Mapping[str, Any]
    dispatch: Mapping[str, Any]


def _frozen_native_value(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen_native_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_native_value(item) for item in value)
    return value


def _native_plain(value):
    if isinstance(value, Mapping):
        return {key: _native_plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_native_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class NativeServiceClaim:
    """Private native claim result, never an ordinary job/API projection."""
    job: Mapping[str, Any]
    checkpoint: Mapping[str, Any]
    binding: RuntimeCompositionBinding
    host_boot_nonce: str
    _host: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _NativeServiceClaimRequest:
    host: Any
    reviewed: Any
    host_boot_nonce: str
    native_report_candidate: Any = field(default=None, repr=False, compare=False)
    header_budget: Any = field(default=None, repr=False, compare=False)
    native_memory_admission: Any = field(default=None, repr=False, compare=False)

    def validate_host(self):
        if (not self.host.admitting or self.host.reviewed is not self.reviewed
                or self.host.boot_nonce != self.host_boot_nonce):
            raise DurableJobLeaseError("original native service host changed before claim")


async def _validate_native_service_claim(db, run, request):
    from src.auth.service import authenticate_principal
    from src.db.models import OperatorSession
    from src.runtime_plugins.ownership import validate_invocation
    request.validate_host()
    binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
    producers = {
        "workflow": {("tasks.admit", "workflow"), ("capabilities.invoke", "workflow"),
                     ("tasks.admit", "artifact"), ("capabilities.invoke", "artifact")},
        "readonly_research_child": {("research.executeAccepted", "public_research"),
                                    ("source-extraction.extract", "public_research")},
        "research_dossier": {("research.executeAccepted", "public_research")},
        "work.local-evidence-report.v1": {("tasks.admit", "artifact")},
        "conversation_turn_v1": {("conversation.accept", "direct_turn"),
                                 ("conversation.accept", "generic_turn")},
        "runtime_service_read_v1": {(method, branch) for branch in {"base", "artifact"} for method in
            {"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"}},
        "runtime_service_memory_v1": {(method, "base") for method in
            {"memory.propose", "memory.applyReviewed", "memory.forget"}},
    }
    if (binding.origin_method, binding.native_branch) not in producers.get(run.job_kind, set()):
        raise DurableJobLeaseError("native service claim producer unsupported")
    if (binding.host_package_digest != request.reviewed.package_digest
            or binding.host_composition_digest != request.reviewed.composition_digest
            or binding.host_package_digest is None or run.deadline_at is None):
        raise DurableJobLeaseError("original reviewed native service binding unavailable")
    memory_budget = request.header_budget if run.job_kind == "runtime_service_memory_v1" else None
    if run.job_kind == "runtime_service_memory_v1":
        from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION
        if type(memory_budget) is not HeaderReadBudget:
            raise DurableJobLeaseError("native_memory_source_budget_unavailable")
        await memory_budget.certify(db, OPERATOR_SESSION, (run.operator_session_id,))
    await validate_invocation(db, binding, **(
        {"header_budget": memory_budget} if memory_budget is not None else {}))
    now = _utc_now()
    if run.owner_kind != "user" or run.operator_session_id != run.session_id:
        raise DurableJobLeaseError("native service original operator provenance unavailable")
    root = await db.scalar(select(OperatorSession).where(
        OperatorSession.id == run.operator_session_id,
        OperatorSession.principal_id == run.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now
    ).execution_options(populate_existing=True))
    if root is None:
        raise DurableJobLeaseError("native service original operator inactive")
    if memory_budget is not None:
        from src.runtime_plugins.memory_producer import certify_original_memory_principal
        await certify_original_memory_principal(db, run.owner_principal_id, memory_budget)
    current_operator = await authenticate_principal(run.owner_principal_id, db=db)
    if run.job_kind == "work.local-evidence-report.v1":
        from src.runtime_plugins.task_capability import recheck_report_claim
        await recheck_report_claim(db, run, host_boot_nonce=request.host_boot_nonce,
            candidate=request.native_report_candidate)
    if run.job_kind == "runtime_service_read_v1":
        from src.runtime_plugins.read_journal import read_context
        if read_context(run)["host_boot_nonce"] != request.host_boot_nonce:
            raise DurableJobLeaseError("native_read_original_host_boot_changed")
        if json.loads(run.declared_authority_json).get("grants") != sorted(
            str(getattr(grant, "value", grant)) for grant in current_operator.principal.grants):
            raise DurableJobLeaseError("native_read_original_policy_changed")
    if run.job_kind == "runtime_service_memory_v1":
        from src.runtime_plugins.memory_producer import memory_context, NativeMemoryMutationAdmission, validate_original_memory_owner
        context = memory_context(run)
        if (context["candidate"]["host_boot_nonce"] != request.host_boot_nonce
            or json.loads(run.declared_authority_json).get("grants") != sorted(
                str(getattr(grant, "value", grant)) for grant in current_operator.principal.grants)):
            raise DurableJobLeaseError("native_memory_original_policy_or_host_changed")
        if context["result"] is None:
            admission = request.native_memory_admission
            if (type(admission) is not NativeMemoryMutationAdmission
                or admission.header_budget is not memory_budget
                or admission.candidate() != context["candidate"]):
                raise DurableJobLeaseError("native_memory_original_admission_unavailable")
            await validate_original_memory_owner(db, admission)
    if run.job_kind == "conversation_turn_v1":
        from src.db.models import Session
        conversation = await db.get(Session, run.conversation_id, populate_existing=True)
        if conversation is None or conversation.continuity_task_id is not None:
            raise DurableJobLeaseError("native_turn_continuity_context_unsupported")
    return binding

@dataclass(frozen=True)
class NativePhysicalCleanupBinding:
    """Internal exact original resource identity; never a browser proof body."""
    job_id: str
    expected_revision: int
    original_owner_principal_id: str
    original_operator_session_id: str
    original_session_id: str
    input_digest: str
    authority_digest: str
    run_fingerprint: str
    attempt_count: int
    lease_owner: str
    fencing_token: int
    resource_claim: str
    witness_digest: str


@dataclass(frozen=True)
class NativePhysicalCleanupProof:
    """Actual resource-owner callback result, not operator-asserted evidence."""
    witness_digest: str
    proof_kind: str
    succeeded_adoption_verified: bool = False


@dataclass(frozen=True)
class ConnectedSourcePhysicalCleanupOwner:
    """Fixed source owner: local proof recheck and same-writer pointer release."""
    verify_cleanup: Callable[..., Awaitable[NativePhysicalCleanupProof]]
    release_pointer: Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class BrowserPhysicalCleanupOwner:
    """Fixed browser owner: nonblocking local lane proof recheck only."""
    verify_cleanup: Callable[..., Awaitable[NativePhysicalCleanupProof]]


def native_physical_cleanup_binding_payload(binding: NativePhysicalCleanupBinding) -> dict[str, Any]:
    return {key: value for key, value in vars(binding).items() if key != "expected_revision"}


DURABLE_JOB_RECORD_SCHEMA_VERSION = 2

# A repair execution reservation is a safety boundary rather than ordinary
# checkpoint history.  It must survive bounded history churn until the exact
# same job/attempt/fence publishes cleanup and readback proof.  Keep the
# latest receipt for each of these ids when trimming any checkpoint history.
_REPO_REPAIR_RESERVATION_CHECKPOINT_IDS = frozenset(
    {
        "repo-repair-execution-reservation",
        "repo-repair-execution-release",
        "native-physical-resource-reservation",
        "native-physical-resource-cleanup",
    }
)

_RUNTIME_SERVICE_CLAIM_PREFIX = "runtime-service-invocation:"


def _protected_composition_checkpoint(checkpoint_id):
    value = _text(checkpoint_id)
    return (value == "runtime-service-invocation" or value.startswith(_RUNTIME_SERVICE_CLAIM_PREFIX)
            or value in {"conversation:assistant-message", "conversation:controlled-outcome", "conversation:operation-family",
                "conversation:cancel", "conversation:callback-closure",
                "memory:original-reference.v2", "memory:current-reference.v2",
                "inference:owned-output-intent.v1", "inference:owned-output.v1", "inference:original-candidate.v1"})


@asynccontextmanager
async def get_session(*, header_budget=None):
    """Resolve the shared session factory through the durable-state module.

    Keeping this narrow proxy makes the canonical repository's database
    dependency explicit and patchable in isolated process/database fixtures
    while preserving the runtime migration hook that owns the session factory.
    """
    from src.workflows import durable_state

    async with durable_state.get_session(**(
        {"header_budget": header_budget} if header_budget is not None else {})) as db:
        db.info["composition_writer_owner"] = "durable_jobs"
        yield db

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


def _native_physical_witness(kind: str, witness: Any, binding: NativePhysicalCleanupBinding) -> None:
    """Closed native platform provenance; fixed owner checks physical truth."""
    source_common = {"platform", "boot_id", "pid", "runtime_nonce", "connection_id",
                     "scope_digest", "original_cursor_revision"}
    source_linux = source_common | {"pid_start_ticks", "pid_namespace"}
    source_darwin = source_common | {"pid_start_sec", "pid_start_usec"}
    browser = {"schema_version", "pid", "process_nonce", "context_nonce", "job_digest",
               "root_path_digest", "root_device", "root_inode", "lock_device", "lock_inode",
               "boot_platform", "boot_session_id", "positive_cleanup_required"}
    if not isinstance(witness, dict):
        raise DurableJobTransitionError("native physical witness schema changed")
    platform = witness.get("platform" if kind == "connection_source_sync" else "boot_platform")
    if not isinstance(platform, str) or platform not in {"linux", "darwin"}:
        raise DurableJobTransitionError("native physical platform is unknown")
    expected = (source_linux if platform == "linux" else source_darwin) if kind == "connection_source_sync" else browser
    if set(witness) != expected:
        raise DurableJobTransitionError("native physical witness schema changed")
    import uuid
    boot = witness["boot_id"] if kind == "connection_source_sync" else witness["boot_session_id"]
    try:
        if not isinstance(boot, str) or str(uuid.UUID(boot)) != boot or uuid.UUID(boot).int == 0:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise DurableJobTransitionError("native physical boot identity is invalid") from None
    integers = (["pid", "original_cursor_revision", "pid_namespace"] if platform == "linux" else
                ["pid", "original_cursor_revision", "pid_start_sec", "pid_start_usec"]) if kind == "connection_source_sync" else ["pid", "root_device", "root_inode", "lock_device", "lock_inode"]
    for key in integers:
        minimum = 1 if key in {"pid", "pid_namespace", "pid_start_sec", "root_inode", "lock_inode"} else 0
        if type(witness[key]) is not int or witness[key] < minimum:
            raise DurableJobTransitionError("native physical integer identity is invalid")
    if witness["pid"] > 2147483647:
        raise DurableJobTransitionError("native physical PID is invalid")
    nonces = ["runtime_nonce"] if kind == "connection_source_sync" else ["process_nonce", "context_nonce"]
    if any(not isinstance(witness[key], str) or re.fullmatch(r"[0-9a-f]{32}", witness[key]) is None for key in nonces):
        raise DurableJobTransitionError("native physical owner nonce is invalid")
    if kind == "connection_source_sync":
        if (not isinstance(witness["connection_id"], str) or not 0 < len(witness["connection_id"]) <= 128
            or binding.resource_claim != "connection-sync:" + witness["connection_id"]
            or not isinstance(witness["scope_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", witness["scope_digest"]) is None):
            raise DurableJobTransitionError("native source scope witness is invalid")
        if platform == "linux" and (not isinstance(witness["pid_start_ticks"], str)
            or re.fullmatch(r"[0-9]{1,32}", witness["pid_start_ticks"]) is None):
            raise DurableJobTransitionError("native Linux process witness is invalid")
        if platform == "darwin" and witness["pid_start_usec"] >= 1000000:
            raise DurableJobTransitionError("native Darwin process witness is invalid")
    elif (type(witness["schema_version"]) is not int or witness["schema_version"] != 4
          or witness["positive_cleanup_required"] is not True
          or witness["job_digest"] != hashlib.sha256(binding.job_id.encode()).hexdigest()
          or not isinstance(witness["root_path_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", witness["root_path_digest"]) is None):
        raise DurableJobTransitionError("native browser scope witness is invalid")


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
    from src.workflows.general_task_guard import protected_checkpoint_ids
    protected_ids = protected_checkpoint_ids(items)
    protected_ids |= {item.get("checkpoint_id") for item in items if isinstance(item, Mapping)
        and item.get("checkpoint_id") in {"document-capacity", "document-child", "document-reaped"}}
    document_children = [item for item in items if isinstance(item, Mapping)
        and item.get("checkpoint_id") == "document-child"]
    if len(document_children) > 1 or any(type(item.get("payload")) is not dict for item in document_children):
        raise DurableJobTransitionError("malformed document process child checkpoint")
    latest_special: dict[str, tuple[int, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        checkpoint_id = _text(item.get("checkpoint_id"))
        if _protected_composition_checkpoint(checkpoint_id):
            if checkpoint_id in latest_special:
                raise DurableJobTransitionError("protected native claim checkpoint duplicated")
            latest_special[checkpoint_id] = (index, item)
        if checkpoint_id in _REPO_REPAIR_RESERVATION_CHECKPOINT_IDS or checkpoint_id in protected_ids:
            latest_special[checkpoint_id] = (index, item)
    special_indexes = {index for index, _item in latest_special.values()}
    ordinary = [
        (index, item)
        for index, item in enumerate(items)
        if index not in special_indexes
    ]
    retained_special = list(latest_special.values())
    if len(retained_special) > bounded_limit:
        if protected_ids:
            raise DurableJobTransitionError("protected checkpoint capacity reached")
        raise DurableJobTransitionError("protected native claim history bound reached")
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


def _general_task_approval_wait(run) -> bool:
    if run.job_kind != "agent.task.v1":
        return False
    checkpoints = _json_load(run.checkpoint_receipts_json, [])
    return run.failure_reason == "general_task_approval_required" or any(
        isinstance(item, dict) and isinstance(item.get("payload"), dict)
        and item["payload"].get("phase") == "approval_precontact"
        for item in checkpoints if isinstance(checkpoints, list))


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


def native_external_effect_state(run: WorkflowRunState) -> str:
    """Redacted canonical liability projection; physical cleanup changes none."""
    if run.job_kind not in {"connection_source_sync", "browser_interact_v2"}:
        raise DurableJobTransitionError("native external-state projection kind is invalid")
    effects = _effect_ledger_or_raise(run.effect_receipts_json)
    if _job_has_unsafe_effects(effects):
        return "unknown"
    return "settled" if effects else "none"


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


def _job_effect_ledger(run, effects):
    # The fixed publication has up to 2,000 finite blob intents. Dropping old
    # settled rows loses its complete recovery inventory. Other jobs retain
    # the established generic history policy.
    if run.job_kind in {"engineering.repo-publication.v1", "github_followthrough_v1"}:
        if len(effects) > 4096 or len(_canonical(effects).encode()) > 4 * 1024 * 1024:
            raise DurableJobTransitionError("publication complete effect inventory limit")
        return effects
    return _bounded_effect_ledger(effects)


def _github_recovery_history(run, history, *, kind):
    """Fixed native histories append within a finite cap; never evict proof."""
    if run.job_kind in {"engineering.repo-publication.v1", "github_followthrough_v1"}:
        if not isinstance(history, list) or len(history) > 4096 or len(_canonical(history).encode()) > 4 * 1024 * 1024:
            raise DurableJobTransitionError("GitHub " + kind + " history bound reached")
        return history
    return _bounded_checkpoint_receipts(history) if kind == "checkpoint" else history[-100:]


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
        if isinstance(details, dict) and details.get("never_contacted") is True:
            continue
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


def _native_turn_pending(run) -> bool:
    if run.job_kind != "conversation_turn_v1" or not run.composition_binding_json:
        return False
    history = _json_load(run.checkpoint_receipts_json, [])
    if type(history) is not list:
        return True
    claims = [item for item in history if isinstance(item, dict)
        and _text(item.get("checkpoint_id")).startswith(_RUNTIME_SERVICE_CLAIM_PREFIX)]
    if run.attempt_count > 0 and len(claims) != 1:
        return True
    cancellation = [item for item in history if isinstance(item, dict)
        and item.get("checkpoint_id") == "conversation:cancel"
        and (not isinstance(item.get("payload"), dict)
            or item["payload"].get("producer") != "native-turn-control-reservation.v1")]
    if cancellation or _text(run.failure_reason).startswith("native_turn_cancel"):
        from src.agent.native_turn_controls import terminal_cancel_projection_valid
        return not terminal_cancel_projection_valid(run, history)
    arguments = _json_load(run.arguments_json, {})
    outputs = []
    for item in history:
        if not isinstance(item, dict):
            continue
        value = item.get("payload")
        if item.get("checkpoint_id") == "conversation:controlled-outcome":
            common = {"schema_version", "input_message_ref", "no_learning", "outcome"}
            reference = "message_ref" if isinstance(value, dict) and value.get("outcome") == "clarification_required" else "approval_ref"
            if (isinstance(value, dict) and set(value) == common | {reference}
                and value.get("outcome") in {"clarification_required", "approval_required"}
                and type(value["schema_version"]) is int and value["schema_version"] == 1
                and type(value[reference]) is str and value["input_message_ref"] == arguments.get("message_ref")
                and value["no_learning"] is True and item.get("safe") is True
                and item.get("state_digest") == _digest(value)):
                outputs.append(item)
            continue
        if item.get("checkpoint_id") != "conversation:assistant-message":
            continue
        if (isinstance(value, dict) and set(value) == {"schema_version", "message_ref", "input_message_ref", "no_learning"}
            and type(value["schema_version"]) is int and value["schema_version"] == 1
            and type(value["message_ref"]) is str and value["input_message_ref"] == arguments.get("message_ref")
            and value["no_learning"] is True and item.get("safe") is True
            and item.get("state_digest") == _digest(value)):
            outputs.append(item)
    if claims and outputs:
        try:
            from src.workspace.accounting_witness import checked_turn_family, RETAINED_FIELDS
            family = checked_turn_family({field: getattr(run, field) for field in RETAINED_FIELDS["workflow_run_states"]})
            if family is None:
                return True
        except Exception:
            return True
    return bool(claims) and not outputs


def _restart_recovery_state(run: WorkflowRunState) -> tuple[str, str]:
    """Classify a stale run without assuming an external callback was harmless."""
    try:
        effects = _effect_ledger_or_raise(getattr(run, "effect_receipts_json", None))
    except DurableJobTransitionError:
        return "blocked", "malformed_effect_history_requires_reconciliation"
    if _job_has_unsafe_effects(effects):
        status, reason = _effect_recovery_state(effects)
        return status, f"stale_lease_{reason}"
    if _native_turn_pending(run):
        return "unknown_external_effect", "native_turn_physical_completion_unproven"
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
    native_research_projection=None, native_job_kind=None,
) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get("authority_type") == "goal_programme_discovery_v1":
        from src.work_board.research_parent import discovery_authority
        return discovery_authority(value).model_dump(mode="json")
    safe = _safe_structure(value)
    if not isinstance(safe, dict) or not isinstance(value, Mapping):
        return safe if isinstance(safe, dict) else {}
    if "legacy_recovery_parent" in value:
        from src.workflows.durable_state import _LEGACY_COMMITMENT_FIELDS
        commitment = value["legacy_recovery_parent"]
        if (type(commitment) is not dict or set(commitment) != _LEGACY_COMMITMENT_FIELDS
            or any(type(item) not in (str, int, type(None))
                or (type(item) is str and len(item.encode("utf-8")) > 512)
                for item in commitment.values())):
            raise ValueError("workflow legacy protected commitment malformed")
        safe["legacy_recovery_parent"] = dict(commitment)
    if native_research_projection is not None:
        from src.work_board.research_parent import NativeResearchProjection, strategy_projection
        if (type(native_research_projection) is not NativeResearchProjection
            or native_job_kind not in {"research_dossier", "readonly_research_child"}
            or not native_research_projection.matches(value, native_job_kind)):
            raise ValueError("native research projection mismatch")
        safe["task_strategy_binding"] = strategy_projection(value["task_strategy_binding"])
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


def _safe_durable_inputs(inputs: Any, *, discovery=False) -> tuple[str, dict[str, Any]]:
    if discovery:
        from src.work_board.research_parent import GoalDiscoveryInputs
        typed = GoalDiscoveryInputs.model_validate(inputs).model_dump(mode="json")
        return _digest(typed), typed
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
    if identity.job_kind == "goal_public_discovery_v1":
        from src.work_board.research_parent import discovery_authority, DISCOVERY_SERVICE
        programme_authority = discovery_authority(authority)
        if (identity.owner_kind != "service" or identity.owner_principal_id != DISCOVERY_SERVICE
                or spec.service_id != DISCOVERY_SERVICE or spec.session_id is not None
                or spec.operator_session_id is not None or spec.parent_job_id
                or programme_authority.original_job_id != identity.job_id
                or programme_authority.programme_binding.goal_id != spec.goal_id
                or programme_authority.programme_binding.goal_revision != spec.goal_revision):
            raise ValueError("discovery requires its original native service lineage")
    elif authority.get("authority_type") == "goal_programme_discovery_v1":
        raise ValueError("programme authority cannot authorize another job kind")
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
    from src.workflows.durable_state import _CURRENT_LEGACY_RECOVERY, _legacy_writer_budget
    legacy_source = _CURRENT_LEGACY_RECOVERY.get()
    if legacy_source is not None:
        from src.memory.header_bounds import GOAL
        await _legacy_writer_budget(db, legacy_source).certify(db, GOAL, (str(goal_id),))
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
    authority_value = authority if isinstance(authority, Mapping) else _json_load(authority, {})
    if isinstance(authority_value, Mapping) and authority_value.get("authority_type") == "goal_programme_discovery_v1":
        from src.workflows.research_guard import assert_discovery_authority
        from src.work_board.research_parent import discovery_authority, DISCOVERY_SERVICE
        native = discovery_authority(authority_value)
        if (owner_kind != "service" or owner_principal_id != DISCOVERY_SERVICE or session_id is not None
                or native.programme_binding.goal_id != goal_id
                or native.programme_binding.goal_revision != goal_revision):
            raise DurableJobTransitionError("programme canonical Goal binding changed")
        await assert_discovery_authority(db, authority_value)
    return goal


def _append_goal_fence_condition(conditions: list[Any], run: WorkflowRunState) -> None:
    """Repeat the canonical goal fence in the final durable row CAS."""
    binding_json = getattr(run, "composition_binding_json", None)
    if binding_json is not None:
        from src.runtime_plugins.ownership import RuntimeCompositionBinding
        from src.db.models import RuntimeCompositionState
        binding = RuntimeCompositionBinding.from_json(binding_json)
        conditions.append(WorkflowRunState.composition_binding_json == binding_json)
        for dependency in binding.dependency_vector:
            conditions.append(select(RuntimeCompositionState.runtime_domain).where(
                RuntimeCompositionState.runtime_domain == dependency.runtime_domain,
                RuntimeCompositionState.owner_kind == dependency.owner_kind,
                RuntimeCompositionState.epoch == dependency.epoch,
                RuntimeCompositionState.composition_digest == dependency.composition_digest,
                RuntimeCompositionState.state == "ready").exists())
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


async def _verify_native_child_sql_scope(db, run):
    """Compile a private canonical journal witness before every child CAS."""
    from src.work_board.communication_preparation import assert_preparation_run_current
    await assert_preparation_run_current(db, run)
    from src.workflows.durable_state import _verify_legacy_child_in_session
    await _verify_legacy_child_in_session(db, run)
    if getattr(run, "job_kind", None) == "general_task_native_tool_v1":
        from src.workflows.general_task_guard import assert_general_task_child_phase_current
        await assert_general_task_child_phase_current(db, run)
    elif getattr(run, "job_kind", None) == "agent.task.v1":
        from src.workflows.specialist_delegation import is_specialist_root, assert_specialist_root_current
        if is_specialist_root(run):
            await assert_specialist_root_current(db, run)


def _append_parent_fence_condition(
    conditions: list[Any], run: WorkflowRunState, *, now: datetime, writer_db=None
) -> None:
    """Require canonical goal identity and, for children, the live parent fence."""
    _append_goal_fence_condition(conditions, run)
    from src.workflows.durable_state import _append_legacy_parent_condition
    if _append_legacy_parent_condition(conditions, run, writer_db=writer_db, now=now):
        return
    if getattr(run, "job_kind", None) == "agent.task.v1":
        from src.workflows.general_task_guard import append_general_task_root_gate
        append_general_task_root_gate(conditions, run, now=now)
        from src.workflows.specialist_delegation import append_specialist_parent_gate
        if append_specialist_parent_gate(conditions, run, now=now):
            return
    if getattr(run, "job_kind", None) == "readonly_research_child":
        from src.workflows.research_guard import append_research_parent_gate
        if append_research_parent_gate(conditions, run, now=now):
            return
    if getattr(run, "job_kind", None) == "general_task_native_tool_v1":
        from src.workflows.general_task_guard import append_general_task_parent_gate
        if append_general_task_parent_gate(conditions, run, now=now):
            return
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


def _composition_fingerprint(spec, input_digest):
    value = _text(spec.run_fingerprint, input_digest)
    if spec.composition_binding is not None:
        if not isinstance(spec.composition_binding, RuntimeCompositionBinding):
            raise DurableJobTransitionError("typed composition binding required")
        return _digest({"native_fingerprint": value,
                        "composition_binding_digest": spec.composition_binding.binding_digest})
    return value


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
        "source_task_id": spec.source_task_id,
        "composition_binding_json": spec.composition_binding.to_json() if spec.composition_binding is not None else None,
    }
    if spec.run_fingerprint is not None or hasattr(existing, "run_fingerprint"):
        expected["run_fingerprint"] = _composition_fingerprint(spec, input_digest)
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
        "source_task_id": getattr(existing, "source_task_id", None),
        "composition_binding_json": getattr(existing, "composition_binding_json", None),
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


def _discovery_constructor_numeric_bounds(*, programme, binding, identifier, now,
                                          deadline, strategy, composition_binding):
    """Original constructor sizes only: no future ref, authority, spec or row.

    The original Guard authenticates these current owner inputs and reserves the
    complete continuation. This pure arithmetic supplies no permission and is
    never a row certificate. Its scalar widths are checked against actual binds
    after the existing constructor and callback have produced the real row.
    """
    from types import MappingProxyType
    from uuid import UUID
    from src.db import models
    from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
    from src.work_board.contracts import TaskStrategyBinding
    from src.work_board.research_parent import DISCOVERY_KIND, DISCOVERY_SERVICE, DISCOVERY_CAPABILITY
    from src.guardian.research_plan_contracts import STAGES, ArtifactRef, GoalResearchPlanSpecV1
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    from src.memory.header_bounds import WRS_BY_RUN, HeaderBoundsError
    if (type(programme) is not GoalProgramme or type(binding) is not GoalProgrammeAuthorityBinding
            or type(identifier) is not UUID or type(strategy) is not TaskStrategyBinding
            or type(composition_binding) is not RuntimeCompositionBinding
            or GoalProgrammeAuthorityBinding.from_programme(programme, DISCOVERY_CAPABILITY) != binding
            or composition_binding.origin_method != "research.executeAccepted"
            or composition_binding.native_branch != "public_research"
            or now.tzinfo is None or deadline.tzinfo is None or not now < deadline <= programme.expires_at):
        raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
    encoded = lambda value: len(_canonical(value).encode("utf-8"))
    def object_bytes(fields):
        if any(type(size) is not int or size < 0 for size in fields.values()):
            raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
        return 2 + sum(encoded(key) + 1 + size for key, size in fields.items()) + max(0, len(fields) - 1)
    def array_bytes(sizes):
        return 2 + sum(sizes) + max(0, len(sizes) - 1)
    # Resource grammar: values below are encoded widths, never reference values.
    reference = object_bytes({"artifact_id": 28 + 2, "digest": 64 + 2, "schema_version": 1})
    if set(ArtifactRef.model_fields) != {"artifact_id", "digest", "schema_version"}:
        raise HeaderBoundsError("programme_constructor_schema_changed")
    path_bytes = len("goal-programmes/") + 32 + 1 + 64 + 1 + 64 + len(".json")
    path = path_bytes + 2  # Original namespace and filename are entirely ASCII.
    job_id = "goal-discovery:" + identifier.hex
    timestamp = 34  # Quoted original timezone-aware ISO8601, at most32 ASCII.
    input_refs = [array_bytes([reference]), encoded([{"producer_step_id": "plan_queries", "output_slot": "queries", "json_pointer": ""}]),
        encoded([{"producer_step_id": "search_public", "output_slot": name, "json_pointer": ""}
            for name in ("manifest", "selection")]),
        encoded([{"producer_step_id": "extract_sources", "output_slot": "snapshots", "json_pointer": ""}])]
    caps = {"queries": 16384, "manifest": 65536, "selection": 8192, "snapshots": 65536, "brief": 65536}
    steps = []
    for index, (name, capability, outputs) in enumerate(STAGES):
        steps.append(object_bytes({"step_id": encoded(name), "capability_id": encoded(capability),
            "capability_version": 1, "input_refs": input_refs[index],
            "output_slots": encoded([{"slot": slot, "artifact_type": kind, "max_bytes": caps[slot]}
                for slot, kind in outputs])}))
    plan_fields = {"schema_version": 1, "plan_id": 38, "programme_id": 38,
        "programme_revision": encoded(programme.grant_revision), "goal_id": encoded(binding.goal_id),
        "goal_revision": encoded(binding.goal_revision), "grant_id": encoded(programme.id),
        "grant_revision": encoded(programme.grant_revision), "public_brief_digest": 66,
        "route_epoch": encoded(programme.route_epoch), "strategy_binding": encoded(strategy.model_dump(mode="json")),
        "issued_at": timestamp, "deadline_at": timestamp, "idempotency_key": 38,
        "limits": encoded({"max_queries": 3, "max_results": 15, "max_sources": 4, "max_inference_requests": 4,
            "max_wall_seconds": 300, "max_search_seconds": 20, "max_search_bytes": 524288,
            "max_source_bytes": 262144, "max_output_bytes": 65536,
            "cost_limit_microusd": programme.budget.max_inference_microusd}), "steps": array_bytes(steps)}
    if set(plan_fields) != set(GoalResearchPlanSpecV1.model_fields):
        raise HeaderBoundsError("programme_constructor_schema_changed")
    plan = object_bytes(plan_fields)
    brief = len(programme.public_brief.encode("utf-8"))
    if not 0 < brief <= 8000 or not 0 < plan <= 65536:
        raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
    inputs = object_bytes({"plan_ref": reference, "plan_file_path": path,
        "public_brief_ref": reference, "public_brief_file_path": path, "no_learning": 4})
    authority_fields = {"authority_type": encoded("goal_programme_discovery_v1"),
        "principal": encoded(DISCOVERY_SERVICE), "owner_kind": encoded("service"),
        "service_id": encoded(DISCOVERY_SERVICE), "capability_id": encoded(DISCOVERY_CAPABILITY),
        "capability_version": encoded("1"), "goal_owner_principal_id": encoded(binding.issuer_principal_id),
        "goal_owner_session_id": encoded(binding.issuer_root_id),
        "programme_binding": encoded(binding.model_dump(mode="json")), "plan_ref": reference,
        "occurrence_day": 12, "original_job_id": encoded(job_id),
        "budget_microusd": encoded(binding.cost_ceiling_microusd), "no_learning": 4}
    from src.work_board.research_parent import GoalDiscoveryAuthority
    if set(authority_fields) != set(GoalDiscoveryAuthority.model_fields):
        raise HeaderBoundsError("programme_constructor_schema_changed")
    authority = object_bytes(authority_fields)
    checkpoints, artifacts, effects = [], [], []
    for kind, size in (("public_brief", brief), ("plan", plan)):
        payload = object_bytes({"artifact_ref": reference, "file_path": path, "kind": encoded(kind),
            "slot": 1, "job_id": encoded(job_id), "programme_id": encoded(programme.id),
            "byte_count": len(str(size)), "producer_fence": 1, "no_learning": 4})
        checkpoints.append(object_bytes({"checkpoint_id": encoded(f"discovery:artifact:{kind}:0"),
            "payload": payload, "recorded_at": timestamp}))
        artifacts.append(object_bytes({"artifact_id": 30, "artifact_type": encoded("goal_discovery_" + kind),
            "file_path": path, "content_sha256": 66, "size_bytes": len(str(size)),
            "producer": encoded(DISCOVERY_KIND), "exists": 4}))
        effects.append(object_bytes({"effect_id": len("discovery-artifact:") + 28 + 2,
            "receipt_kind": encoded("readback"), "effect_type": encoded("research_artifact_readback"),
            "status": encoded("succeeded"), "target_path": path, "content_sha256": 66,
            "target_digest": 66, "verified_at": timestamp,
            "readback_id": len("discovery-readback-") + 28 + 2, "reconciled": 4,
            "reconciliation_status": encoded("resolved"), "details": encoded({"verified": True, "no_learning": True})}))
    # Each entry is a scalar UTF8 width, not a speculative SQL/domain row.
    # The original queue may durably fail an expired just-admitted occurrence;
    # its finished_at bind needs the same fixed SQLite timestamp width.
    text = {"id": 32, "run_identity": len(job_id), "root_run_identity": len(job_id),
        "workflow_name": len(DISCOVERY_KIND), "tool_name": len(DISCOVERY_KIND), "status": len("accepted"),
        "run_fingerprint": 64, "arguments_json": inputs, "approval_context_json": authority,
        "artifact_paths_json": 2, "continued_error_steps_json": 2,
        "heartbeat_at": 26, "started_at": 26, "updated_at": 26, "finished_at": 26,
        "job_kind": len(DISCOVERY_KIND), "owner_kind": len("service"),
        "owner_principal_id": len(DISCOVERY_SERVICE), "service_id": len(DISCOVERY_SERVICE),
        "goal_id": len(binding.goal_id.encode("utf-8")), "composition_binding_json": len(composition_binding.to_json().encode()),
        "capability_version": 1, "input_digest": 64, "authority_digest": 64, "budget_digest": 64,
        "idempotency_scope": len("goal-programme-daily"), "idempotency_key": 32, "idempotency_binding": 64,
        "dependencies_json": 2, "resource_claims_json": encoded(["goal-discovery:" + binding.owner_identity_id]),
        "declared_authority_json": authority, "deadline_at": 26, "failure_reason": len("deadline_expired"),
        "checkpoint_receipts_json": array_bytes(checkpoints), "artifact_receipts_json": array_bytes(artifacts),
        "effect_receipts_json": array_bytes(effects)}
    integer = {"branch_depth", "record_schema_version", "goal_revision", "priority", "fencing_token", "revision", "attempt_count", "max_attempts"}
    null = {"parent_run_identity", "session_id", "conversation_id", "operator_session_id", "branch_kind",
        "checkpoint_context_json", "last_completed_step_id", "error", "metadata_json",
        "parent_job_id", "parent_fencing_token", "plan_revision", "candidate_id", "source_task_id",
        "selected_context_reserved_bytes", "lease_owner", "lease_expires_at", "github_read_revision_json",
        "github_read_observation_history_json", "github_capacity_closure_json", "result_digest", "result_summary"}
    if set(WRS_BY_RUN.columns) != set(text) | integer | null or set(WorkflowRunState.model_fields) != set(WRS_BY_RUN.columns):
        raise HeaderBoundsError("programme_constructor_schema_changed")
    expected_defaults = {"branch_kind": None, "artifact_paths_json": "[]", "continued_error_steps_json": "[]",
        "last_completed_step_id": None, "error": None, "finished_at": None, "metadata_json": None,
        "lease_owner": None, "lease_expires_at": None, "fencing_token": 0, "revision": 0, "attempt_count": 0,
        "github_read_revision_json": None, "github_read_observation_history_json": None,
        "github_capacity_closure_json": None, "result_digest": None, "result_summary": None}
    if (any(WorkflowRunState.model_fields[name].default != value for name, value in expected_defaults.items())
            or WorkflowRunState.model_fields["id"].default_factory is not models._uuid
            or any(WorkflowRunState.model_fields[name].default_factory is not models._now
                for name in ("heartbeat_at", "started_at", "updated_at"))):
        raise HeaderBoundsError("programme_constructor_schema_changed")
    skeleton = ["native-composition-memory.v1", WRS_BY_RUN.table, job_id, [[name, None] for name in WRS_BY_RUN.columns]]
    upper = encoded(skeleton) + 128
    for name, kind, nullable in zip(WRS_BY_RUN.columns, WRS_BY_RUN.kinds, WRS_BY_RUN.nullable):
        if name in text:
            if kind != "text": raise HeaderBoundsError("programme_constructor_schema_changed")
            upper += 6 * text[name] + 2 - 4
        elif name in integer:
            if kind != "integer": raise HeaderBoundsError("programme_constructor_schema_changed")
            upper += 20 - 4
        elif not nullable:
            raise HeaderBoundsError("programme_constructor_schema_changed")
    return MappingProxyType({"job_id": job_id, "brief_bytes": brief, "plan_bytes": plan,
        "input_bytes": inputs, "authority_bytes": authority, "checkpoint_bytes": text["checkpoint_receipts_json"],
        "artifact_bytes": text["artifact_receipts_json"], "effect_bytes": text["effect_receipts_json"],
        "row_header_bytes": upper, "text_bytes": MappingProxyType(text),
        "integer_columns": frozenset(integer), "null_columns": frozenset(null)})


def _validate_discovery_constructor_numeric_bounds(run, bounds):
    """Check the ACTUAL original constructor binds against numerical widths.

    This is resource validation only. Current authority, physical provenance,
    native composition and writer scope remain the original owners' checks.
    """
    from sqlalchemy.dialects.sqlite import dialect
    from src.memory.header_bounds import WRS_BY_RUN, HeaderBoundsError
    if (type(run) is not WorkflowRunState or run.run_identity != bounds["job_id"]
            or set(bounds["text_bytes"]) | set(bounds["integer_columns"]) | set(bounds["null_columns"])
                != set(WRS_BY_RUN.columns)):
        raise HeaderBoundsError("programme_constructor_numeric_bind_changed")
    selected_dialect = dialect()
    for name, kind, nullable in zip(WRS_BY_RUN.columns, WRS_BY_RUN.kinds, WRS_BY_RUN.nullable):
        column = WorkflowRunState.__table__.columns[name]
        processor = column.type.dialect_impl(selected_dialect).bind_processor(selected_dialect)
        value = getattr(run, name)
        value = processor(value) if processor is not None and value is not None else value
        if value is None:
            if not nullable:
                raise HeaderBoundsError("programme_constructor_numeric_bind_changed")
        elif name in bounds["text_bytes"]:
            if kind != "text" or type(value) is not str or len(value.encode("utf-8")) > bounds["text_bytes"][name]:
                raise HeaderBoundsError("programme_constructor_numeric_bind_changed")
        elif name in bounds["integer_columns"]:
            if kind != "integer" or type(value) is not int or not -(2**63) <= value < 2**63:
                raise HeaderBoundsError("programme_constructor_numeric_bind_changed")
        else:
            raise HeaderBoundsError("programme_constructor_numeric_bind_changed")


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
        "source_task_id": getattr(run, "source_task_id", None),
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
        "checkpoints": [item for item in _json_load(getattr(run, "checkpoint_receipts_json", None), [])
            if not isinstance(item, dict) or not _protected_composition_checkpoint(item.get("checkpoint_id"))],
        "artifacts": _json_load(getattr(run, "artifact_receipts_json", None), []),
        "effects": _json_load(getattr(run, "effect_receipts_json", None), []),
        "started_at": run.started_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "claim_boundary": "durable_job_contract_on_workflow_state_not_exactly_once_external_execution",
    }
    if receipt is not None:
        payload["receipt"] = receipt
    if _native_turn_pending(run):
        payload["native_turn_execution"] = {"phase": "possibly_started", "physical_completion": "unproven", "replay": "denied"}
    if getattr(run, "github_capacity_closure_json", None):
        from src.extensions.github_capacity_closure import public_closure
        payload["github_capacity_closure"] = public_closure(run.github_capacity_closure_json)
    return payload


def _native_memory_pending_output(run, pending_changes, *, receipt):
    """Original serializer over actual pending binds, for numeric reserve only."""
    from sqlalchemy.dialects.sqlite import dialect
    from src.workspace.accounting_witness import _native_memory_planned_sql_row
    # The closed binder validates all loaded original fields and exact pending
    # values. This temporary view is never a mapped row or an authority handle.
    bound = _native_memory_planned_sql_row(run, pending_changes)
    selected_dialect = dialect()
    projected = {}
    for name in pending_changes:
        column = WorkflowRunState.__table__.columns[name]
        processor = column.type.dialect_impl(selected_dialect).result_processor(selected_dialect, None)
        projected[name] = processor(bound[name]) if processor is not None and bound[name] is not None else bound[name]

    class _NumericProjection:
        def __getattr__(self, name):
            if name in projected:
                return projected[name]
            return getattr(run, name)

    return _serialize(_NumericProjection(), receipt=receipt)


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
    source_task_id: str | None = None
    composition_binding: RuntimeCompositionBinding | None = None
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

    @asynccontextmanager
    async def _writer_session(self, *, header_budget=None):
        maintenance = _original_memory_maintenance_scope(self)
        if maintenance is not None and maintenance.header_budget is not None:
            if header_budget is not None and header_budget is not maintenance.header_budget:
                raise DurableJobLeaseError("original_memory_maintenance_frame_changed")
            header_budget = maintenance.header_budget
        async with self._session() as db:
            if getattr(db, "info", {}).get("composition_read_guard") is not None:
                from src.workspace.accounting_witness import prepare_composition_session
                await prepare_composition_session(db, **(
                    {"header_budget": header_budget} if header_budget is not None else {}))
                db.info["composition_writer_owner"] = "durable_jobs"
            elif header_budget is not None:
                from src.memory.composition_headers import _certify_current_memory_snapshot
                connection = await db.connection()
                if not (await connection.get_raw_connection()).driver_connection.in_transaction:
                    await db.execute(text("BEGIN IMMEDIATE" if maintenance is not None else "BEGIN"))
                await _certify_current_memory_snapshot(db, header_budget)
            if maintenance is not None:
                await _original_memory_maintenance_snapshot(self, db, writer=True)
            yield db

    async def reserve_native_physical_resource(
        self, binding: NativePhysicalCleanupBinding, *, witness: dict[str, Any],
        current_owner, authenticated_token_hash: str,
    ) -> dict[str, Any]:
        """Persist typed original provenance through normal execution fences."""
        if type(binding) is not NativePhysicalCleanupBinding:
            raise DurableJobTransitionError("native physical reservation binding is invalid")
        payload = {"binding": native_physical_cleanup_binding_payload(binding), "witness": witness}
        return await self.record_checkpoint(binding.job_id,
            checkpoint_id="native-physical-resource-reservation", state=payload, checkpoint_payload=payload,
            owner=binding.lease_owner, fencing_token=binding.fencing_token,
            expected_revision=binding.expected_revision,
            native_physical_reservation=(binding, current_owner, authenticated_token_hash))

    async def record_native_physical_cleanup(
        self, binding: NativePhysicalCleanupBinding, *, current_owner,
        authenticated_token_hash: str,
        proof_kind: str,
        cleanup_owner: ConnectedSourcePhysicalCleanupOwner | BrowserPhysicalCleanupOwner,
    ) -> dict[str, Any]:
        """Release exact native physical capacity without reconciling effects.

        Fixed resource owners retain the physical lock while their callback
        checks actual teardown. Pointer changes compose in this writer; external
        locks may be released only after this transaction returns committed.
        Original Root/Goal validity is deliberately irrelevant to negative local
        cleanup. Their immutable provenance and current bearer ownership are not.
        """
        from src.db.models import OperatorIdentity, OperatorSession

        if (type(binding) is not NativePhysicalCleanupBinding
            or type(binding.expected_revision) is not int or binding.expected_revision < 1
            or type(binding.attempt_count) is not int or binding.attempt_count < 1
            or type(binding.fencing_token) is not int or binding.fencing_token < 1
            or not isinstance(authenticated_token_hash, str) or not authenticated_token_hash):
            raise DurableJobTransitionError("native physical cleanup binding is invalid")
        async with self._writer_session() as db:
            if db.get_bind().dialect.name == "sqlite":
                await _begin_legacy_aware_writer(db)
            run = await self._fetch(db, binding.job_id)
            kinds = {"connection_source_sync": "connection-sync-v1", "browser_interact_v2": "2"}
            eligible = {"connection_source_sync": {"running", "unknown_external_effect", "cost_liability", "failed", "succeeded"},
                        "browser_interact_v2": {"running", "unknown_external_effect", "cost_liability"}}
            expected = native_physical_cleanup_binding_payload(binding)
            actual = {
                "job_id": run.run_identity, "original_owner_principal_id": run.owner_principal_id,
                "original_operator_session_id": run.operator_session_id,
                "original_session_id": run.session_id, "input_digest": run.input_digest,
                "authority_digest": run.authority_digest, "run_fingerprint": run.run_fingerprint,
                "attempt_count": run.attempt_count, "lease_owner": binding.lease_owner,
                "fencing_token": run.fencing_token, "resource_claim": binding.resource_claim,
                "witness_digest": binding.witness_digest,
            }
            authority = _json_load(run.declared_authority_json, {})
            if (run.job_kind not in kinds or run.capability_version != kinds[run.job_kind]
                or run.owner_kind != "user" or actual != expected
                or run.status not in eligible.get(run.job_kind, set())
                or run.operator_session_id != run.session_id
                or _revision(run) != binding.expected_revision
                or run.lease_owner not in {None, binding.lease_owner}
                or not binding.lease_owner or not binding.run_fingerprint
                or not isinstance(authority, dict) or _digest(authority) != run.authority_digest
                or authority.get("principal") != run.owner_principal_id
                or authority.get("session_id") != run.operator_session_id
                or authority.get("goal_id") != run.goal_id or authority.get("goal_revision") != run.goal_revision
                or _json_load(run.resource_claims_json, []) != [binding.resource_claim]):
                raise DurableJobLeaseError("native original physical reservation changed")
            if (run.job_kind == "browser_interact_v2" and
                (binding.resource_claim != "browser-task-lane" or authority.get("capability_id") != "browser.interact.v2")):
                raise DurableJobTransitionError("native browser cleanup kind changed")
            if (run.job_kind == "connection_source_sync" and
                (binding.resource_claim != "connection-sync:" + str(authority.get("connection_id", ""))
                 or authority.get("capability_id") not in {"mail.messages.read", "calendar.events.read"})):
                raise DurableJobTransitionError("native source cleanup kind changed")
            current = await db.get(OperatorSession, current_owner.session_id)
            original = await db.get(OperatorSession, binding.original_operator_session_id)
            now = _utc_now()
            if (current is None or current.principal_id != current_owner.principal_id
                or current.token_hash != authenticated_token_hash or current.revoked_at is not None
                or current.replaced_by_id is not None or current.is_bearer_tombstone
                or _as_utc(current.idle_expires_at) <= now or _as_utc(current.absolute_expires_at) <= now
                or original is None or original.principal_id != binding.original_owner_principal_id
                or not original.operator_identity_id or original.operator_identity_id != current.operator_identity_id):
                raise DurableJobTransitionError("native cleanup authenticated operator changed")
            identity = await db.get(OperatorIdentity, original.operator_identity_id)
            if identity is None or identity.revoked_at is not None:
                raise DurableJobTransitionError("native cleanup stable operator is unavailable")
            history = _json_load(run.checkpoint_receipts_json, None)
            if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
                raise DurableJobTransitionError("native physical journal is malformed")
            reservations = [item for item in history if item.get("checkpoint_id") == "native-physical-resource-reservation"]
            if len(reservations) != 1:
                raise DurableJobTransitionError("native original physical reservation is missing")
            reservation = reservations[0].get("payload")
            if (not isinstance(reservation, dict) or set(reservation) != {"binding", "witness"}
                or reservation["binding"] != expected or not isinstance(reservation["witness"], dict)
                or _digest(reservation["witness"]) != binding.witness_digest
                or reservations[0].get("fencing_token") != binding.fencing_token
                or reservations[0].get("safe") is not True
                or reservations[0].get("state_digest") != _digest(reservation)):
                raise DurableJobTransitionError("native original physical witness changed")
            _native_physical_witness(run.job_kind, reservation["witness"], binding)
            # A well-formed empty ledger remains none, never settlement proof.
            effect_state = native_external_effect_state(run)
            platform = reservation["witness"]["platform" if run.job_kind == "connection_source_sync" else "boot_platform"]
            allowed = {"owned_positive_close", platform + "_boot_changed"}
            if run.job_kind == "connection_source_sync":
                allowed.add("positive_process_death")
                if type(cleanup_owner) is not ConnectedSourcePhysicalCleanupOwner or not callable(cleanup_owner.release_pointer):
                    raise DurableJobTransitionError("native source cleanup owner is required")
            elif type(cleanup_owner) is not BrowserPhysicalCleanupOwner:
                raise DurableJobTransitionError("native browser cleanup owner is required")
            else:
                allowed.add("owned_no_child")
            if proof_kind == "owned_no_child" and effect_state != "none":
                raise DurableJobTransitionError("native prechild cleanup has contact evidence")
            if not isinstance(proof_kind, str) or proof_kind not in allowed or not callable(cleanup_owner.verify_cleanup):
                raise DurableJobTransitionError("native physical proof kind is invalid")
            receipt = {"kind": "native_physical_cleanup", "scope": "physical_cleanup_only",
                       "witness_digest": binding.witness_digest, "proof_kind": proof_kind,
                       "binding_digest": _digest(expected), "no_learning": True}
            prior = [item for item in history if item.get("checkpoint_id") == "native-physical-resource-cleanup"]
            if prior:
                if (len(prior) != 1 or prior[0].get("payload") != receipt or prior[0].get("safe") is not True
                    or prior[0].get("state_digest") != _digest(receipt) or prior[0].get("fencing_token") != binding.fencing_token):
                    raise DurableJobTransitionError("native physical cleanup replay changed")
                return {"job_id": binding.job_id, "revision": _revision(run),
                        "status": run.status, "receipt": {**receipt, "deduped": True}}
            # Fixed owners do only bounded local/DB rechecks here. Actual task
            # awaiting and lane acquisition complete before entering this writer.
            proof = await cleanup_owner.verify_cleanup(db, run, reservation)
            if (type(proof) is not NativePhysicalCleanupProof or proof.witness_digest != binding.witness_digest
                or proof.proof_kind != proof_kind or type(proof.succeeded_adoption_verified) is not bool
                or proof.succeeded_adoption_verified is not (run.job_kind == "connection_source_sync" and run.status == "succeeded")):
                raise DurableJobTransitionError("native positive physical cleanup is unproven")
            checkpoint = {"checkpoint_id": "native-physical-resource-cleanup", "safe": True,
                          "fencing_token": binding.fencing_token, "recorded_at": now.isoformat(),
                          "state_digest": _digest(receipt), "payload": receipt}
            history.append(checkpoint)
            changed = await db.execute(update(WorkflowRunState).where(
                WorkflowRunState.id == run.id, WorkflowRunState.revision == binding.expected_revision,
                WorkflowRunState.fencing_token == binding.fencing_token,
                WorkflowRunState.attempt_count == binding.attempt_count,
            ).values(checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts(history)), revision=WorkflowRunState.revision + 1))
            if changed.rowcount != 1:
                raise DurableJobLeaseError("native physical cleanup CAS is stale")
            if type(cleanup_owner) is ConnectedSourcePhysicalCleanupOwner:
                await cleanup_owner.release_pointer(db, run)
            await db.commit()
            return {"job_id": binding.job_id, "revision": binding.expected_revision + 1,
                    "status": run.status, "receipt": receipt}

    async def complete_mail_observation_in_session(
        self, db, run, *, owner: str, fencing_token: int, observation: Mapping[str, Any],
    ) -> None:
        """Compose fixed readonly Mail recovery with its original-row CAS.

        The Mail writer validates original provenance and current Root in the
        same transaction. This seam retains the auxiliary's ordinary current
        Goal, native lease, deadline, kind and revision fences, and never
        finalizes or changes the original send job.
        """
        if (run.job_kind != "mail_reply_observation_v1"
            or run.capability_version != "mail-exact-reply-v1" or run.status != "running"
            or observation.get("outcome") not in {"verified_sent_observation", "unknown_observation"}
            or observation.get("no_learning") is not True
            or observation.get("auxiliary_job_id") != run.run_identity):
            raise DurableJobTransitionError("exact Mail observation binding is required")
        self._assert_lease(run, owner=owner, fencing_token=fencing_token)
        if _deadline_expired(run):
            raise DurableJobTransitionError("job deadline has expired")
        await _assert_canonical_goal_fence(db, goal_id=run.goal_id,
            goal_revision=run.goal_revision, owner_kind=run.owner_kind,
            owner_principal_id=run.owner_principal_id, session_id=run.session_id,
            authority=run.declared_authority_json)
        # This readonly kind cannot carry caller-seeded evidence obligations.
        # Inspect canonical dependencies without filesystem work in the writer.
        if _json_load(run.dependencies_json, []) or _job_has_unsafe_effects(
            _effect_ledger_or_raise(run.effect_receipts_json)):
            raise DurableJobTransitionError("Mail observation retains unresolved dependencies or effects")
        checkpoint = _json_load(run.checkpoint_context_json, {})
        private_ref = observation.get("private_artifact")
        if (not isinstance(private_ref, dict) or set(private_ref) != {"path", "digest"}
            or checkpoint.get("private_artifacts", {}).get("observation") != private_ref):
            raise DurableJobTransitionError("Mail observation artifact reservation changed")
        # Physical encryption/publish/readback and actual awaited transport
        # completion were staged by the fixed Mail worker before this writer.
        checkpoint.update(outcome=observation["outcome"], transport_quiescent=True)
        artifacts = [{"artifact_type": "mail_exact_reply", "file_path": private_ref["path"],
            "content_sha256": private_ref["digest"], "exists": True, "no_learning": True}]
        now = _utc_now()
        conditions = [WorkflowRunState.id == run.id,
            WorkflowRunState.revision == _revision(run), WorkflowRunState.status == "running",
            WorkflowRunState.lease_owner == owner, WorkflowRunState.lease_expires_at > now,
            WorkflowRunState.fencing_token == fencing_token]
        _append_goal_fence_condition(conditions, run)
        changed = await db.execute(update(WorkflowRunState).where(*conditions).values(
            status="succeeded", result_digest=_digest(observation),
            artifact_receipts_json=_canonical(artifacts), checkpoint_context_json=_canonical(checkpoint),
            result_summary=observation["outcome"], finished_at=now, updated_at=now,
            heartbeat_at=now, lease_owner=None, lease_expires_at=None,
            revision=WorkflowRunState.revision + 1).execution_options(synchronize_session=False))
        if not _rowcount_is_one(changed):
            raise DurableJobLeaseError("Mail observation changed before atomic completion")

    async def complete_calendar_observation_in_session(
        self, db, run, *, owner: str, fencing_token: int, observation: Mapping[str, Any],
    ) -> None:
        """Compose fixed readonly Calendar recovery with its original-row CAS.

        The Calendar writer validates original provenance and current Root in the
        same transaction. This seam retains the auxiliary's ordinary current
        Goal, native lease, deadline, kind and revision fences, and never
        finalizes or changes the original reschedule job.
        """
        if (run.job_kind != "calendar_reschedule_observation_v1"
            or run.capability_version != "calendar-exact-reschedule-v1" or run.status != "running"
            or observation.get("outcome") not in {"verified_reschedule_observation", "unknown_observation"}
            or observation.get("no_learning") is not True
            or observation.get("auxiliary_job_id") != run.run_identity):
            raise DurableJobTransitionError("exact Calendar observation binding is required")
        self._assert_lease(run, owner=owner, fencing_token=fencing_token)
        if _deadline_expired(run):
            raise DurableJobTransitionError("job deadline has expired")
        await _assert_canonical_goal_fence(db, goal_id=run.goal_id,
            goal_revision=run.goal_revision, owner_kind=run.owner_kind,
            owner_principal_id=run.owner_principal_id, session_id=run.session_id,
            authority=run.declared_authority_json)
        # This readonly kind cannot carry caller-seeded evidence obligations.
        # Inspect canonical dependencies without filesystem work in the writer.
        if _json_load(run.dependencies_json, []) or _job_has_unsafe_effects(
            _effect_ledger_or_raise(run.effect_receipts_json)):
            raise DurableJobTransitionError("Calendar observation retains unresolved dependencies or effects")
        checkpoint = _json_load(run.checkpoint_context_json, {})
        private_ref = observation.get("private_artifact")
        if (not isinstance(private_ref, dict) or set(private_ref) != {"path", "digest"}
            or checkpoint.get("private_artifacts", {}).get("observation") != private_ref):
            raise DurableJobTransitionError("Calendar observation artifact reservation changed")
        # Physical encryption/publish/readback and actual awaited transport
        # completion were staged by the fixed Calendar worker before this writer.
        checkpoint.update(outcome=observation["outcome"], transport_quiescent=True)
        artifacts = [{"artifact_type": "calendar_exact_reschedule", "file_path": private_ref["path"],
            "content_sha256": private_ref["digest"], "exists": True, "no_learning": True}]
        now = _utc_now()
        conditions = [WorkflowRunState.id == run.id,
            WorkflowRunState.revision == _revision(run), WorkflowRunState.status == "running",
            WorkflowRunState.lease_owner == owner, WorkflowRunState.lease_expires_at > now,
            WorkflowRunState.fencing_token == fencing_token]
        _append_goal_fence_condition(conditions, run)
        changed = await db.execute(update(WorkflowRunState).where(*conditions).values(
            status="succeeded", result_digest=_digest(observation),
            artifact_receipts_json=_canonical(artifacts), checkpoint_context_json=_canonical(checkpoint),
            result_summary=observation["outcome"], finished_at=now, updated_at=now,
            heartbeat_at=now, lease_owner=None, lease_expires_at=None,
            revision=WorkflowRunState.revision + 1).execution_options(synchronize_session=False))
        if not _rowcount_is_one(changed):
            raise DurableJobLeaseError("Calendar observation changed before atomic completion")

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

    async def admit_job(self, spec: DurableJobSpec, **kwargs) -> dict[str, Any]:
        return await self._admit_in_session(None, spec, **kwargs)

    async def admit_workflow_recovery_job(self, spec, *, source):
        from dataclasses import replace
        from src.workflows.durable_state import (_read_legacy_parent_in_session,
            _LEGACY_COMMITMENT, _CURRENT_LEGACY_RECOVERY)
        if source is not _CURRENT_LEGACY_RECOVERY.get():
            raise DurableJobAdmissionDenied("workflow_legacy_original_producer_unavailable")
        async with self._writer_session() as db:
            await _begin_legacy_aware_writer(db)
            source.child_identity = spec.identity.job_id
            verified = await _read_legacy_parent_in_session(db, source=source)
            parent = verified.parent
            if spec.identity.job_kind != parent.workflow_name or spec.declared_authority.get("capability") != parent.tool_name:
                raise DurableJobAdmissionDenied("workflow_legacy_original_workflow_changed")
            authority = {**spec.declared_authority, **verified.contract,
                _LEGACY_COMMITMENT: verified.commitment}
            original_authority = _json_load(parent.declared_authority_json, {})
            for key in ("budget_microusd", "max_budget_microusd", "owner_cost_budget_microusd", "budget"):
                authority.pop(key, None)
                if key in original_authority:
                    authority[key] = original_authority[key]
            parent_cutoff = _as_utc(verified.commitment["metadata_lease_expires_at"])
            if parent.deadline_at is not None:
                parent_cutoff = min(parent_cutoff, _as_utc(parent.deadline_at))
            authority["deadline_at"] = parent_cutoff.isoformat()
            authority["dependencies"] = _string_list(_json_load(parent.dependencies_json, []))
            source.commitment = verified.commitment
            spec = replace(spec, **{key: verified.contract[key] for key in
                ("goal_id", "goal_revision", "plan_revision", "candidate_id")},
                declared_authority=authority, parent_job_id=parent.run_identity,
                parent_fencing_token=None, session_id=parent.session_id,
                conversation_id=parent.conversation_id or parent.session_id,
                operator_session_id=parent.operator_session_id,
                deadline_at=parent_cutoff,
                dependencies=tuple(authority["dependencies"]),
                budget_microusd=_authority_budget_microusd(_json_load(parent.declared_authority_json, {})),
                priority=parent.priority, max_attempts=parent.max_attempts)
            return await self._admit_in_session(db, spec, legacy_recovery_source=source)

    @asynccontextmanager
    async def _admission_session(self, db, *, legacy_source=None):
        if db is not None:
            # The caller owns this ONE transaction and its rollback. No
            # intermediate commit/rollback may detach Message from its job.
            from src.workflows.durable_state import _CURRENT_LEGACY_RECOVERY
            fixed_legacy_writer = (legacy_source is not None
                and legacy_source is _CURRENT_LEGACY_RECOVERY.get()
                and legacy_source._live.is_set())
            if not db.in_transaction() or (not db.info.get("native_writer_started") and not fixed_legacy_writer):
                raise DurableJobTransitionError("native admission writer required")
            yield db
        else:
            async with self._writer_session() as session:
                yield session

    async def _admit_in_session(
        self, admission_db, spec: DurableJobSpec, *,
        admission_dependencies=None,
        repo_node_posture_expectation: dict[str, Any] | None = None,
        selected_context_admission=None,
        admission_authority_check: Callable[[Any, Any], Awaitable[None]] | None = None,
        opportunity_preference_witness=None,
        near_text_policy_scope=None,
        native_turn_admission=None,
        native_read_admission=None,
        native_read_host=None,
        native_read_host_boot_nonce=None,
        legacy_recovery_source=None,
        native_memory_admission=None,
        native_memory_host=None,
        native_memory_host_boot_nonce=None,
        native_task_admission=None,
        native_task_host=None,
        native_task_host_boot_nonce=None,
        native_research_projection=None,
    ) -> dict[str, Any]:
        # Internal server-only copy of actual selected Node preflight facts.
        # A separate method argument cannot be supplied by spec/request
        # serialization and is never persisted as durable authority itself.
        identity = spec.identity
        from src.workflows.durable_state import _LEGACY_COMMITMENT, _LegacyRecoverySource
        if _LEGACY_COMMITMENT in spec.declared_authority and type(legacy_recovery_source) is not _LegacyRecoverySource:
            raise DurableJobAdmissionDenied("workflow_legacy_original_producer_unavailable")
        if identity.job_kind == "work.local-evidence-report.v1" and spec.composition_binding is not None:
            from src.runtime_plugins.task_capability import ReportAdmissionCandidate
            from src.runtime_plugins.bridge import CordisHost
            if (type(native_task_admission) is not ReportAdmissionCandidate
                or native_task_admission.original_spec is not spec
                or spec.composition_binding.origin_method != "tasks.admit"
                or spec.composition_binding.native_branch != "artifact"
                or not spec.composition_binding.allows("capabilities.invoke")):
                raise DurableJobAdmissionDenied("native_report_original_admission_required")
            if (type(native_task_host) is not CordisHost or not native_task_host.admitting
                or native_task_host.reviewed is None or native_task_host.boot_nonce != native_task_host_boot_nonce
                or type(native_task_host_boot_nonce) is not str or not re.fullmatch(r"[0-9a-f]{64}", native_task_host_boot_nonce)
                or spec.composition_binding.host_package_digest != native_task_host.reviewed.package_digest
                or spec.composition_binding.host_composition_digest != native_task_host.reviewed.composition_digest):
                raise DurableJobAdmissionDenied("native_report_original_host_changed")
        elif any(value is not None for value in (native_task_admission, native_task_host, native_task_host_boot_nonce)):
            raise DurableJobAdmissionDenied("native_report_admission_unexpected")
        if identity.job_kind == "runtime_service_memory_v1":
            from src.runtime_plugins.memory_producer import NativeMemoryMutationAdmission
            from src.runtime_plugins.bridge import CordisHost
            if (type(native_memory_admission) is not NativeMemoryMutationAdmission
                or spec.inputs != native_memory_admission.candidate()
                or admission_authority_check is None or admission_db is None):
                raise DurableJobAdmissionDenied("native_memory_original_admission_required")
            if (type(native_memory_host) is not CordisHost or not native_memory_host.admitting
                or native_memory_host.reviewed is None or spec.composition_binding is None
                or native_memory_host.boot_nonce != native_memory_host_boot_nonce
                or native_memory_host_boot_nonce != native_memory_admission.candidate()["host_boot_nonce"]
                or spec.composition_binding.host_package_digest != native_memory_host.reviewed.package_digest
                or spec.composition_binding.host_composition_digest != native_memory_host.reviewed.composition_digest):
                raise DurableJobAdmissionDenied("native_memory_original_host_changed")
        elif any(value is not None for value in (native_memory_admission, native_memory_host, native_memory_host_boot_nonce)):
            raise DurableJobAdmissionDenied("native_memory_admission_unexpected")
        if identity.job_kind == "runtime_service_read_v1":
            from src.runtime_plugins.read_admission import NativeServiceReadAdmission
            from src.runtime_plugins.bridge import CordisHost
            if type(native_read_admission) is not NativeServiceReadAdmission:
                raise DurableJobAdmissionDenied("native_read_admission_required")
            if spec.inputs != native_read_admission.candidate():
                raise DurableJobAdmissionDenied("native_read_original_candidate_changed")
            if admission_authority_check is None or admission_db is None:
                raise DurableJobAdmissionDenied("native_read_original_writer_required")
            if (type(native_read_host) is not CordisHost or not native_read_host.admitting
                or native_read_host.boot_nonce != native_read_host_boot_nonce
                or type(native_read_host_boot_nonce) is not str or not re.fullmatch(r"[0-9a-f]{64}", native_read_host_boot_nonce)
                or native_read_host.reviewed is None or spec.composition_binding is None
                or spec.composition_binding.host_package_digest != native_read_host.reviewed.package_digest
                or spec.composition_binding.host_composition_digest != native_read_host.reviewed.composition_digest):
                raise DurableJobAdmissionDenied("native_read_original_host_changed")
        elif native_read_admission is not None or native_read_host is not None or native_read_host_boot_nonce is not None:
            raise DurableJobAdmissionDenied("native_read_admission_unexpected")
        if identity.job_kind == "conversation_turn_v1":
            from src.agent.turn_execution import NativeTurnAdmission
            if not isinstance(native_turn_admission, NativeTurnAdmission):
                raise DurableJobAdmissionDenied("native_turn_admission_required")
            fields = {"schema_version", "message_ref", "content_digest", "native_route", "native_timeout_seconds"}
            if type(spec.inputs) is not dict or set(spec.inputs) != fields or spec.inputs != native_turn_admission.inputs:
                raise DurableJobAdmissionDenied("native_turn_input_projection_changed")
        elif native_turn_admission is not None:
            raise DurableJobAdmissionDenied("native_turn_admission_unexpected")
        if identity.job_kind == "general_task_native_tool_v1":
            from src.workflows.general_task_guard import is_fixed_child_admission
            if not is_fixed_child_admission(admission_authority_check):
                raise DurableJobAdmissionDenied("general_task_fixed_native_admission_required")
        if near_text_policy_scope is not None:
            from src.work_board.near_text_native import validate_native_policy_scope
            validate_native_policy_scope(near_text_policy_scope, run_or_identity=identity, phase="admit")
        if identity.job_kind == "selected_context_v1":
            from src.workflows.selected_context_runtime import AdmissionProof
            if not isinstance(selected_context_admission, AdmissionProof) or not spec.source_task_id:
                raise DurableJobAdmissionDenied("selected_context_proof_required")
        elif selected_context_admission is not None or spec.source_task_id is not None:
            raise DurableJobAdmissionDenied("selected_context_proof_unexpected")
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
        input_digest, safe_inputs = _safe_durable_inputs(spec.inputs, discovery=identity.job_kind == "goal_public_discovery_v1")
        if native_read_admission is not None:
            input_digest = native_read_admission.candidate_digest
        if native_memory_admission is not None:
            input_digest = native_memory_admission.candidate_digest
        if native_turn_admission is not None:
            safe_inputs = dict(spec.inputs)
        run_fingerprint = _bounded_identifier(
            _composition_fingerprint(spec, input_digest),
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
        from src.work_board.research_contracts import PARENT_KIND as research_parent_kind
        if identity.job_kind == research_parent_kind:
            if native_research_projection is None:
                raise DurableJobAdmissionDenied("research_original_projection_required")
        elif native_research_projection is not None:
            raise DurableJobAdmissionDenied("research_original_projection_unexpected")
        safe_authority = _safe_durable_authority(
            spec.declared_authority,
            native_research_projection=native_research_projection, native_job_kind=identity.job_kind,
            repo_node_posture_expectation=repo_node_posture_expectation,
        )
        root_run_identity = identity.job_id
        branch_depth = 0
        native_procedure_leaf = False
        canonical_goal: Goal | None = None
        async with self._admission_session(admission_db, legacy_source=legacy_recovery_source) as db:
            bind = db.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            from src.memory.evidence_dependencies import stage_run_dependencies, recheck_run_dependencies
            from src.work_board.repository import BoardError
            async def certify_native_memory_body(descriptor):
                if native_memory_admission is not None:
                    await native_memory_admission.header_budget.certify_all(db, descriptor)
            if native_memory_admission is not None:
                from src.memory.header_bounds import WRS_BY_RUN, GOAL, SESSION
                await certify_native_memory_body(WRS_BY_RUN)
            # Existing immutable admission replay does not authorize new use.
            # Stage physical evidence only for a genuinely new invocation and
            # finish those reads before acquiring the canonical writer.
            prior_admission = await db.scalar(select(WorkflowRunState).where(
                WorkflowRunState.idempotency_binding == binding))
            if prior_admission is None and admission_db is None:
                candidate = WorkflowRunState(run_identity=identity.job_id,
                    job_kind=identity.job_kind, owner_principal_id=identity.owner_principal_id,
                    operator_session_id=spec.operator_session_id, goal_id=spec.goal_id,
                    goal_revision=spec.goal_revision, idempotency_key=identity.idempotency_key,
                    declared_authority_json=_canonical(safe_authority))
                try:
                    admission_dependencies = await stage_run_dependencies(db, candidate)
                except (BoardError, OSError, KeyError, TypeError):
                    # The pure canonical guard below commits the stale receipt
                    # for a bound task rather than admitting unreadable input.
                    admission_dependencies = None
            if admission_db is None:
                await db.rollback()
            transaction_started = admission_db is not None
            if selected_context_admission is not None:
                # The capture identity is independent from mutable Goal/Root/
                # Task fields. Its exact primary lookup must precede ordinary
                # Goal-derived dedupe, within this same admission transaction.
                if dialect_name == "sqlite" and not transaction_started:
                    await _begin_legacy_aware_writer(db)
                    transaction_started = True
                from src.workflows.selected_context_runtime import guard_admission
                original = await guard_admission(db, spec, selected_context_admission)
                if original is not None:
                    db.expunge(original)
                    return _deduped_admission(original, binding=original.idempotency_binding)
            if not _text(spec.goal_id) and spec.goal_revision is not None:
                raise DurableJobTransitionError("goal_revision requires a canonical goal")
            if _text(spec.goal_id):
                # The local-first runtime uses SQLite.  Start one immediate
                # write transaction before reading the canonical goal so a
                # goal update/delete cannot race admission. A row-locking
                # backend serializes on the canonical goal row instead.
                if dialect_name == "sqlite" and not transaction_started:
                    await _begin_legacy_aware_writer(db)
                    transaction_started = True
                else:
                    await db.execute(
                        select(Goal.id)
                        .where(Goal.id == spec.goal_id)
                        .with_for_update()
                    )
                if native_memory_admission is not None:
                    await certify_native_memory_body(GOAL)
                canonical_goal = await _assert_canonical_goal_fence(
                    db,
                    goal_id=spec.goal_id,
                    goal_revision=spec.goal_revision,
                    owner_kind=identity.owner_kind,
                    owner_principal_id=identity.owner_principal_id,
                    session_id=spec.session_id,
                    authority=spec.declared_authority,
                )
            if spec.composition_binding is not None:
                if dialect_name == "sqlite" and not transaction_started:
                    await _begin_legacy_aware_writer(db)
                    transaction_started = True
                if dialect_name == "sqlite" and transaction_started and db.info.get("composition_guard") is not None:
                    db.info["native_writer_started"] = True
                from src.runtime_plugins.ownership import validate_invocation
                await validate_invocation(db, spec.composition_binding,
                    **({"header_budget": native_memory_admission.header_budget}
                        if native_memory_admission is not None else {}))
            if native_read_admission is not None:
                from src.runtime_plugins.read_journal import validate_read_spec
                await validate_read_spec(db, spec, native_read_admission)
            if native_task_admission is not None:
                from src.runtime_plugins.task_capability import validate_report_spec
                await validate_report_spec(db, spec, native_task_admission, host_boot_nonce=native_task_host_boot_nonce)
            if native_memory_admission is not None:
                from src.runtime_plugins.memory_producer import validate_memory_spec
                await validate_memory_spec(db, spec, native_memory_admission)
            if native_turn_admission is not None:
                from src.agent.turn_execution import native_turn_spec, validate_native_turn_owner
                await validate_native_turn_owner(db, native_turn_admission)
                if spec != await native_turn_spec(db, native_turn_admission):
                    raise DurableJobAdmissionDenied("native_turn_original_spec_changed")
                from src.db.models import Message
                original_message = await db.get(Message, spec.inputs["message_ref"])
                if (original_message is None or original_message.role != "user"
                    or original_message.owner_principal_id != spec.identity.owner_principal_id
                    or original_message.operator_session_id != spec.operator_session_id
                    or original_message.session_id != spec.conversation_id
                    or hashlib.sha256(original_message.content.encode()).hexdigest() != spec.inputs["content_digest"]):
                    raise DurableJobAdmissionDenied("native_turn_original_message_changed")
            if legacy_recovery_source is not None:
                from src.workflows.durable_state import _read_legacy_parent_in_session
                original = await _read_legacy_parent_in_session(db, source=legacy_recovery_source)
                if spec.declared_authority.get("legacy_recovery_parent") != original.commitment:
                    raise DurableJobAdmissionDenied("workflow_legacy_original_parent_changed")
                root_run_identity = original.parent.root_run_identity or original.parent.run_identity
                branch_depth = int(original.parent.branch_depth) + 1
            if spec.parent_job_id is not None and legacy_recovery_source is None:
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
                    await _begin_legacy_aware_writer(db)
                    transaction_started = True
                await self._assert_routine_publication_admission_guard(
                    db,
                    spec=spec,
                    guard=spec.routine_publication_admission_guard,
                    now=now,
                    dialect_name=dialect_name,
                )
            if (admission_authority_check is not None or identity.job_kind == "memory.opportunity-preference.v1") and dialect_name == "sqlite" and not transaction_started:
                await _begin_legacy_aware_writer(db)
                transaction_started = True
            if dialect_name == "sqlite" and db.info.get("composition_guard") is not None:
                if not transaction_started:
                    await _begin_legacy_aware_writer(db)
                    transaction_started = True
                db.info["native_writer_started"] = True
            if native_memory_admission is not None:
                await certify_native_memory_body(SESSION)
            await ensure_sessions_exist(db, [spec.session_id], retained_native=(spec.composition_binding is not None
                or db.info.get("composition_guard") is not None))
            if native_memory_admission is not None:
                await certify_native_memory_body(WRS_BY_RUN)
            existing = (
                await db.execute(
                    select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == binding)
                )
            ).scalars().first()
            if existing is not None:
                if native_read_admission is not None:
                    from src.runtime_plugins.read_journal import read_context
                    if read_context(existing)["host_boot_nonce"] != native_read_host_boot_nonce:
                        raise DurableJobAdmissionDenied("native_read_original_host_changed")
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

            native_communication_leaf = False
            if "communication_preparation" in spec.declared_authority:
                from src.work_board.communication_preparation import verify_admission_spec
                native_communication_leaf = await verify_admission_spec(db, admission_authority_check, spec)
            if (
                spec.goal_id is not None
                and spec.max_outstanding_jobs is not None
                and not native_procedure_leaf
                and not native_communication_leaf
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

            if native_memory_admission is not None:
                await certify_native_memory_body(WRS_BY_RUN)
            by_id = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity.job_id))
            ).scalars().first()
            if by_id is not None:
                raise DurableJobIdempotencyConflict("job_id already belongs to a different invocation")
            status = "failed" if deadline and deadline <= now else "accepted"
            failure_reason = "deadline_expired" if status == "failed" else None
            private_read_context = None
            if native_read_admission is not None:
                from src.runtime_plugins.read_journal import candidate_context
                if (not native_read_host.admitting or native_read_host.boot_nonce != native_read_host_boot_nonce):
                    raise DurableJobAdmissionDenied("native_read_original_host_changed")
                private_read_context = candidate_context(native_read_admission, host_boot_nonce=native_read_host_boot_nonce,
                    native_branch=spec.composition_binding.native_branch)
                db.info["composition_native_read_context"] = (identity.job_id, private_read_context)
            if native_memory_admission is not None:
                from src.runtime_plugins.memory_producer import candidate_context
                if not native_memory_host.admitting or native_memory_host.boot_nonce != native_memory_host_boot_nonce:
                    raise DurableJobAdmissionDenied("native_memory_original_host_changed")
                private_read_context = candidate_context(native_memory_admission)
                db.info["composition_native_memory_admission"] = native_memory_admission
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
                checkpoint_context_json=(_canonical({"schema_version": 1,
                    "metadata": spec.inputs,
                    "pair_file_digest": selected_context_admission.pair.state_file_digest})
                    if selected_context_admission is not None else private_read_context),
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
                source_task_id=spec.source_task_id,
                composition_binding_json=(spec.composition_binding.to_json()
                    if spec.composition_binding is not None else None),
                selected_context_reserved_bytes=(spec.inputs["reviewed_byte_count"]
                    if selected_context_admission is not None else None),
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
            if identity.job_kind == research_parent_kind:
                from src.work_board.research_parent import recheck_native_admission
                admission = await recheck_native_admission(db, run, native_research_projection)
                run.checkpoint_receipts_json = _canonical([{"checkpoint_id": "research:admission",
                    "payload": admission, "state_digest": _digest(admission), "state_keys": sorted(admission),
                    "safe": True, "fencing_token": 0, "recorded_at": now.isoformat()}])
            await recheck_run_dependencies(db, run, admission_dependencies)
            if identity.job_kind in {"forgejo_issue_title_v1", "inference.near-text.v1", "goal_public_discovery_v1"} and admission_authority_check is None:
                raise DurableJobAdmissionDenied("forgejo_fixed_native_admission_required")
            if identity.job_kind == "guardian_opportunity_assess" and admission_authority_check is None:
                raise DurableJobAdmissionDenied("guardian_opportunity_fixed_native_admission_required")
            if identity.job_kind == "memory.opportunity-preference.v1":
                from src.work_board.opportunity_preference_native import recheck_native
                await recheck_native(db,run,witness=opportunity_preference_witness)
            if admission_authority_check is not None:
                # Server-only capability guard shares the canonical Goal and
                # new-row insert transaction. Exact immutable replay above
                # performs no new authority-bearing admission or callback.
                if near_text_policy_scope is not None:
                    if dialect_name != "sqlite" or not transaction_started:
                        raise DurableJobAdmissionDenied("near_policy_writer_required")
                    from src.work_board.near_text_native import enter_native_policy_scope
                    enter_native_policy_scope(near_text_policy_scope, db=db,
                        run_or_identity=identity, phase="admit")
                await admission_authority_check(db, run)
            if native_read_admission is not None and (
                not native_read_host.admitting or native_read_host.boot_nonce != native_read_host_boot_nonce):
                raise DurableJobAdmissionDenied("native_read_original_host_changed")
            if native_memory_admission is not None and (
                not native_memory_host.admitting or native_memory_host.boot_nonce != native_memory_host_boot_nonce):
                raise DurableJobAdmissionDenied("native_memory_original_host_changed")
            if native_task_admission is not None:
                if not native_task_host.admitting or native_task_host.boot_nonce != native_task_host_boot_nonce:
                    raise DurableJobAdmissionDenied("native_report_original_host_changed")
                from src.runtime_plugins.task_capability import seal_report_admission
                await seal_report_admission(db, run, native_task_admission, host_boot_nonce=native_task_host_boot_nonce)
            if native_memory_admission is not None:
                from src.workspace.accounting_witness import reserve_native_memory_admission_run
                await reserve_native_memory_admission_run(db, run, native_memory_admission)
            db.add(run)
            try:
                await db.flush()
            except IntegrityError as exc:
                # The unique binding is the authoritative concurrent-admission
                # fence.  Re-read after rollback so the losing invocation is
                # idempotent when it supplied the same immutable contract, yet
                # still rejects a conflicting job or identity collision.
                await db.rollback()
                if near_text_policy_scope is not None:
                    # The failed insert published no new authority. Release
                    # only after real rollback, before the immutable reread
                    # can open a fresh database transaction.
                    from src.work_board.near_text_native import release_admission_scope_after_rollback
                    release_admission_scope_after_rollback(near_text_policy_scope, db=db)
                if native_memory_admission is not None:
                    from src.runtime_plugins.ownership import begin_native_writer
                    await begin_native_writer(db, owner="durable_jobs",
                        header_budget=native_memory_admission.header_budget)
                    await certify_native_memory_body(WRS_BY_RUN)
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
                if native_memory_admission is not None:
                    await certify_native_memory_body(WRS_BY_RUN)
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

    async def admit_task_capability_job(self, spec, *, candidate, host,
                                        admission_db=None, admission_authority_check=None):
        """Admit only the source-staged original report specification."""
        return await self._admit_in_session(admission_db, spec,
            native_task_admission=candidate, native_task_host=host,
            native_task_host_boot_nonce=host.boot_nonce,
            admission_authority_check=admission_authority_check)

    async def record_task_capability_checkpoint(self, job_id, *, original_scope, checkpoint_candidate):
        return await self._record_task_capability_phase(job_id, original_scope=original_scope,
            method="tasks.checkpoint", phase="source", candidate=checkpoint_candidate)

    async def record_task_capability_invocation(self, job_id, *, original_scope, invocation_witness):
        return await self._record_task_capability_phase(job_id, original_scope=original_scope,
            method="capabilities.invoke", phase="invoke", candidate=invocation_witness)

    async def _record_task_capability_phase(self, job_id, *, original_scope, method, phase, candidate):
        from src.runtime_plugins.dispatch import NativeServiceDispatcher
        from src.runtime_plugins.task_capability import prepare_report_publication, preflight_report_journal
        if (method, phase) not in {("tasks.checkpoint", "source"), ("capabilities.invoke", "invoke")}:
            raise DurableJobTransitionError("fixed original report phase required")
        async with self._writer_session() as db:
            run, witness, _ = await NativeServiceDispatcher(jobs=self)._current_in_db(
                db, job_id, method, original_scope)
            if run.job_kind != "work.local-evidence-report.v1":
                raise DurableJobTransitionError("fixed original report required")
            original = {field: getattr(run, field) for field in (
                "checkpoint_receipts_json", "artifact_receipts_json", "effect_receipts_json",
                "arguments_json", "declared_authority_json", "input_digest", "authority_digest",
                "run_fingerprint", "composition_binding_json", "deadline_at", "attempt_count")}
            previous = original["checkpoint_receipts_json"]
            receipt = await prepare_report_publication(db, run, phase=phase,
                candidate=candidate, original_scope=original_scope)
            if any(getattr(run, field) != value for field, value in original.items()):
                raise DurableJobTransitionError("native report phase preparation mutated original owner")
            history = json.loads(previous)
            if any(item.get("checkpoint_id") == receipt["checkpoint_id"] for item in history):
                raise DurableJobTransitionError("original report phase cannot be republished")
            history.append(receipt)
            preflight_report_journal(history)
            now = _utc_now()
            changed = await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False).where(
                WorkflowRunState.run_identity == job_id, WorkflowRunState.status == "running",
                WorkflowRunState.revision == run.revision,
                WorkflowRunState.fencing_token == witness["fencing_token"],
                WorkflowRunState.lease_owner == witness["lease_owner"], WorkflowRunState.lease_expires_at > now,
                WorkflowRunState.deadline_at == run.deadline_at, WorkflowRunState.deadline_at > now,
                WorkflowRunState.attempt_count == 1, WorkflowRunState.input_digest == run.input_digest,
                WorkflowRunState.authority_digest == run.authority_digest,
                WorkflowRunState.run_fingerprint == run.run_fingerprint,
                WorkflowRunState.composition_binding_json == run.composition_binding_json,
                WorkflowRunState.checkpoint_receipts_json == previous,
                WorkflowRunState.artifact_receipts_json == run.artifact_receipts_json,
                WorkflowRunState.effect_receipts_json == original["effect_receipts_json"],
                WorkflowRunState.arguments_json == original["arguments_json"],
                WorkflowRunState.declared_authority_json == original["declared_authority_json"],
            ).values(checkpoint_receipts_json=_canonical(history), updated_at=now,
                heartbeat_at=now, revision=WorkflowRunState.revision + 1))
            if not _rowcount_is_one(changed):
                raise DurableJobLeaseError("original report phase changed before publication")
            current = await self._fetch(db, job_id)
            return _serialize(current, receipt={"kind": "native_report_phase", "phase": phase,
                "receipt_ref": receipt["checkpoint_id"], "no_learning": True})

    async def settle_task_capability(self, job_id, *, original_scope, outcome_candidate, terminal_witness):
        from src.runtime_plugins.task_capability import settle_report_job
        return await settle_report_job(self, job_id, original_scope=original_scope,
            outcome_candidate=outcome_candidate, terminal_witness=terminal_witness)

    async def cancel_task_capability(self, job_id, *, original_scope, cancel_candidate,
                                     cleanup_witness, expected_revision):
        from src.runtime_plugins.task_capability import cancel_report_job
        return await cancel_report_job(self, job_id, original_scope=original_scope,
            cancel_candidate=cancel_candidate, cleanup_witness=cleanup_witness,
            expected_revision=expected_revision)

    async def replace_general_task_manifest(self, job_id, **bindings):
        from src.workflows.general_task_guard import replace_manifest
        return await replace_manifest(self, job_id, **bindings)

    async def admit_general_task_tool_child(self, spec, **bindings):
        from src.workflows.general_task_guard import admit_child
        return await admit_child(self, spec, **bindings)

    async def publish_general_task_step_receipt(self, parent_id, **bindings):
        from src.workflows.general_task_guard import publish_step_receipt
        return await publish_step_receipt(self, parent_id, **bindings)

    async def publish_general_task_tool_closure(self, child_id, **bindings):
        from src.workflows.general_task_guard import publish_tool_closure
        return await publish_tool_closure(self, child_id, **bindings)

    async def wait_general_task_native_approval(self, child_id, **bindings):
        from src.workflows.general_task_guard import wait_native_approval
        return await wait_native_approval(self, child_id, **bindings)

    async def resume_general_task_native_approval(self, child_id, **bindings):
        from src.workflows.general_task_guard import resume_native_approval
        return await resume_native_approval(self, child_id, **bindings)

    async def cancel_general_task_native_parent(self, parent_id, **bindings):
        from src.workflows.general_task_guard import cancel_native_parent
        return await cancel_native_parent(self, parent_id, **bindings)

    async def observe_general_task_native_cancel_closure(self, child_id, **bindings):
        from src.workflows.general_task_guard import observe_native_cancel_closure
        return await observe_native_cancel_closure(self, child_id, **bindings)

    async def pause_general_task_native_parent(self, parent_id, **bindings):
        from src.workflows.general_task_guard import pause_parent
        return await pause_parent(self, parent_id, **bindings)

    async def resume_general_task_native_parent(self, parent_id, **bindings):
        from src.workflows.general_task_guard import resume_parent
        return await resume_parent(self, parent_id, **bindings)

    async def revise_general_task_operator_paused_parent(self, parent_id, **bindings):
        from src.workflows.general_task_guard import revise_operator_paused_parent
        return await revise_operator_paused_parent(self, parent_id, **bindings)

    async def get_job(self, job_id: str, *, header_budget=None) -> dict[str, Any] | None:
        maintenance = _original_memory_maintenance_scope(self)
        if maintenance is not None:
            if header_budget is not None and header_budget is not maintenance.header_budget:
                raise DurableJobLeaseError("original_memory_maintenance_frame_changed")
            header_budget = maintenance.header_budget
        async with self._session() as db:
            if maintenance is not None:
                await _original_memory_maintenance_snapshot(self, db)
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                from src.memory.composition_headers import locate_exact_rows
                connection = await db.connection()
                if not (await connection.get_raw_connection()).driver_connection.in_transaction:
                    await db.execute(text("BEGIN"))
                ids = await locate_exact_rows(db, WRS_BY_RUN, job_id, header_budget)
                if not ids:
                    return None
                await header_budget.certify(db, WRS_BY_RUN, ids)
            run = (
                await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            ).scalars().first()
            if run is None:
                return None
            db.expunge(run)
            output = _serialize(run)
            if maintenance is not None and header_budget is not None:
                header_budget.debit(len(_canonical(output).encode("utf-8")))
            return output

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
        workflow_step_id: str | None = None,
    ) -> dict[str, Any]:
        """Re-read a running job and reject a stale or expired lease."""
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            if run.status != "running":
                raise DurableJobLeaseError("durable job is not running")
            self._assert_lease(run, owner=owner, fencing_token=fencing_token)
            authority = _json_load(getattr(run, "declared_authority_json", None), {})
            if not isinstance(authority, dict) or "legacy_recovery_parent" not in authority:
                db.expunge(run)
                return _serialize(run)
            await _begin_legacy_aware_writer(db)
            if isinstance(authority, dict) and "legacy_recovery_parent" in authority:
                from src.memory.header_bounds import strict_json_loads
                effects = strict_json_loads(run.effect_receipts_json)
                checkpoints = strict_json_loads(run.checkpoint_receipts_json)
                intents = [item for item in effects if isinstance(item, dict)
                    and item.get("effect_id") == f"workflow-step:{workflow_step_id}"
                    and item.get("effect_type") == "workflow_step" and item.get("status") == "intent"]
                starts = [item for item in checkpoints if isinstance(item, dict)
                    and item.get("checkpoint_id") == f"step:{workflow_step_id}"]
                if not workflow_step_id or len(intents) != 1 or len(starts) != 1:
                    raise DurableJobLeaseError("workflow original positive step intent required")
            await _assert_canonical_goal_fence(db, goal_id=run.goal_id,
                goal_revision=run.goal_revision, owner_kind=run.owner_kind,
                owner_principal_id=run.owner_principal_id, session_id=run.session_id,
                authority=run.declared_authority_json)
            await _verify_native_child_sql_scope(db, run)
            conditions = [WorkflowRunState.run_identity == run.run_identity,
                WorkflowRunState.revision == run.revision, WorkflowRunState.status == "running",
                WorkflowRunState.lease_owner == owner, WorkflowRunState.fencing_token == fencing_token,
                WorkflowRunState.lease_expires_at > _utc_now()]
            _append_parent_fence_condition(conditions, run, now=_utc_now(), writer_db=db)
            if await db.scalar(select(WorkflowRunState.id).where(*conditions)) is None:
                raise DurableJobLeaseError("workflow current Goal/parent dispatch refused")
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
        async with self._writer_session() as db:
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
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

    @_original_memory_maintenance_entry
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
        cancellation_authority_check: Callable[[Any, Any], Awaitable[None]] | None = None,
        opportunity_preference_witness=None,
        near_text_witness=None,
        near_text_policy_scope=None,
        _general_task_resume_witness=None,
        _native_report_terminal=None,
        header_budget=None,
    ) -> dict[str, Any]:
        if to_status not in DURABLE_JOB_STATUSES:
            raise DurableJobTransitionError(f"unknown durable job status: {to_status}")
        async with self._writer_session(**(
            {"header_budget": header_budget} if header_budget is not None else {})) as db:
            # A capability-specific terminal guard must observe its owner,
            # consent, and artifact rows in the same serialized transaction as
            # the root CAS.  SQLite otherwise permits a stale read snapshot
            # between the caller's last preflight and this transition.
            from src.memory.evidence_dependencies import stage_run_dependencies, recheck_run_dependencies
            staged_dependencies = None
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                await header_budget.certify(db, WRS_BY_RUN, (job_id,))
            preflight_run = await self._fetch(db, job_id)
            report_terminal = (preflight_run.job_kind == "work.local-evidence-report.v1"
                and preflight_run.composition_binding_json is not None
                and to_status in {"succeeded", "degraded", "cancelled"})
            if report_terminal:
                if (_native_report_terminal is None or to_status == "degraded"
                    or (to_status == "succeeded" and terminal_authority_check is None)
                    or (to_status == "cancelled" and cancellation_authority_check is None)):
                    raise DurableJobTransitionError("original native report terminal publication required")
                from src.runtime_plugins.task_capability import validate_report_terminal_request
                validate_report_terminal_request(_native_report_terminal, job_id=job_id, to_status=to_status)
            elif _native_report_terminal is not None:
                raise DurableJobTransitionError("native report terminal publication does not match job")
            if to_status == "queued" and preflight_run.job_kind == "agent.task.v1":
                from src.workflows.general_task_guard import read_manifest
                native_manifest = read_manifest(preflight_run)
                if native_manifest is not None:
                    raise DurableJobTransitionError("native parent phases require the paired manifest resume writer")
            general_resume_guard = (
                to_status == "queued" and _general_task_approval_wait(preflight_run)
            )
            near_queue_guard = preflight_run.job_kind == "inference.near-text.v1" and to_status == "queued"
            if near_queue_guard or near_text_policy_scope is not None:
                if to_status != "queued":
                    raise DurableJobTransitionError("NEAR policy scope is only valid for queueing")
                from src.work_board.near_text_native import validate_native_policy_scope
                validate_native_policy_scope(near_text_policy_scope, run_or_identity=preflight_run,
                    phase="queue", witness=near_text_witness)
            if str(preflight_run.status) in DURABLE_JOB_TERMINAL_STATUSES and cancellation_authority_check is None:
                # Exact historical replay admits no contact/source use. Keep
                # the original canonical Goal fence, then return the existing
                # terminal result before inspecting mutable dependencies.
                if to_status not in {'failed', 'cancelled'}:
                    await _assert_canonical_goal_fence(db, goal_id=preflight_run.goal_id,
                        goal_revision=preflight_run.goal_revision, owner_kind=preflight_run.owner_kind,
                        owner_principal_id=preflight_run.owner_principal_id, session_id=preflight_run.session_id,
                        authority=preflight_run.declared_authority_json)
                if str(preflight_run.status) != to_status:
                    raise DurableJobTransitionError(f'terminal job cannot transition {preflight_run.status} -> {to_status}')
                db.expunge(preflight_run)
                return _serialize(preflight_run, receipt={'kind': 'transition', 'status': 'deduped',
                    'terminal_noop': True, 'revision': _revision(preflight_run)})
            dependency_guard = (preflight_run.job_kind in {'browser_public_task',
                'work.evidence-dossier.v1', 'work.local-evidence-report.v1'}
                and to_status in {'queued', 'running', 'succeeded', 'degraded'})
            guardian_queue_guard = preflight_run.job_kind == "guardian_opportunity_assess" and to_status == "queued"
            preference_guard = preflight_run.job_kind == "memory.opportunity-preference.v1" and to_status in {"queued", "succeeded", "degraded"}
            from src.workflows.general_task_guard import requires_native_writer, verify_native_writer
            native_writer = requires_native_writer(preflight_run)
            if dependency_guard:
                staged_dependencies = await stage_run_dependencies(db, preflight_run)
            await db.rollback()
            maintenance = _original_memory_maintenance_scope(self)
            if maintenance is not None and maintenance.header_budget is not None:
                await _original_memory_maintenance_snapshot(self, db, writer=True)
            near_writer_started = False
            general_writer_started = False
            if header_budget is not None:
                if preflight_run.job_kind != "runtime_service_memory_v1":
                    raise DurableJobTransitionError("native_memory_resource_context_unexpected")
                from src.runtime_plugins.ownership import begin_native_writer
                await begin_native_writer(db, owner="durable_jobs", header_budget=header_budget)
            elif report_terminal:
                from src.runtime_plugins.ownership import begin_native_writer
                await begin_native_writer(db, owner="finite_service")
            elif (terminal_authority_check is not None and to_status in {"succeeded", "degraded"}) or dependency_guard or cancellation_authority_check is not None or guardian_queue_guard or preference_guard or near_queue_guard or general_resume_guard or native_writer:
                bind = db.get_bind()
                dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
                if dialect_name == "sqlite":
                    await _begin_legacy_aware_writer(db)
                    near_writer_started = near_queue_guard
                    general_writer_started = general_resume_guard
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                await header_budget.certify(db, WRS_BY_RUN, (job_id,))
            run = await self._fetch(db, job_id)
            report_original = None
            if report_terminal:
                report_original = {field: getattr(run, field) for field in (
                    "checkpoint_receipts_json", "artifact_receipts_json", "effect_receipts_json",
                    "arguments_json", "declared_authority_json", "input_digest", "authority_digest",
                    "run_fingerprint", "composition_binding_json", "deadline_at", "attempt_count")}
            if native_writer and run.job_kind == "agent.task.v1":
                await verify_native_writer(self, db, run)
            elif native_writer and to_status in {"succeeded", "degraded"}:
                from src.workflows.general_task_guard import assert_general_task_child_terminal_current
                await assert_general_task_child_terminal_current(self, db, run)
            if to_status == "queued" and _general_task_approval_wait(run):
                if not general_writer_started or _general_task_resume_witness is None:
                    raise DurableJobTransitionError(
                        "general task approval resume requires current validated authority"
                    )
                try:
                    from src.work_board.general_task_approval import recheck_resume_witness
                    await recheck_resume_witness(db, run, _general_task_resume_witness)
                except Exception as exc:
                    raise DurableJobTransitionError(
                        "general task approval resume requires current validated authority"
                    ) from exc
            elif _general_task_resume_witness is not None:
                raise DurableJobTransitionError("general task approval witness does not match the paused root")
            if near_queue_guard:
                if not near_writer_started:
                    raise DurableJobTransitionError("near_policy_writer_required")
                from src.work_board.near_text_native import enter_native_policy_scope, recheck_native_queue
                enter_native_policy_scope(near_text_policy_scope, db=db, run_or_identity=run,
                    phase="queue", witness=near_text_witness)
                await recheck_native_queue(db,run,witness=near_text_witness)
            if preference_guard:
                from src.work_board.opportunity_preference_native import recheck_native
                await recheck_native(db,run,witness=opportunity_preference_witness,terminal=to_status in {"succeeded","degraded"})
            if guardian_queue_guard and run.status == "blocked":
                from src.guardian.opportunity_runtime import guard_recovered_queue
                await guard_recovered_queue(db, run)
            if cancellation_authority_check is not None:
                if to_status != "cancelled":
                    raise DurableJobTransitionError("cancellation authority applies only to cancellation")
                await cancellation_authority_check(db, run)
            if dependency_guard:
                await recheck_run_dependencies(db, run, staged_dependencies)
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
            if run.job_kind == "model_inference_ephemeral_v1" and to_status in {"succeeded", "degraded"}:
                from src.runtime_plugins.inference_output import CANDIDATE_ID, OUTPUT_IDS
                if any(item.get("checkpoint_id") in OUTPUT_IDS | {CANDIDATE_ID}
                       for item in _json_load(run.checkpoint_receipts_json, [])):
                    raise DurableJobTransitionError("original inference output requires its private sealing writer")
            if run.job_kind == "runtime_service_read_v1" and to_status == "succeeded":
                raise DurableJobTransitionError("original native read sealed completion required")
            if run.job_kind == "runtime_service_memory_v1" and to_status in {"succeeded", "degraded"}:
                raise DurableJobTransitionError("original native Memory sealed completion required")
            if (run.job_kind == "work.local-evidence-report.v1" and run.composition_binding_json
                and run.attempt_count == 1 and to_status == "failed"):
                # Only the original Task5 settle/cancel owner can prove physical closure.
                # A failed waiter or elapsed lease does not settle the actual file callback.
                to_status = "unknown_external_effect"
                reason = "native_report_original_closure_unproven"
            if run.job_kind == "conversation_turn_v1" and run.composition_binding_json and to_status == "succeeded":
                raise DurableJobTransitionError("original native turn publication required")
            if (run.job_kind == "conversation_turn_v1" and run.composition_binding_json
                and not _native_turn_pending(run) and to_status in {"failed", "cancelled"}):
                await self.assert_native_turn_family_terminal_in_session(db, run)
            if _native_turn_pending(run) and to_status in {"queued", "running", "succeeded"}:
                raise DurableJobTransitionError("original native turn physical completion is unproven; replay denied")
            if _native_turn_pending(run) and to_status in {"failed", "cancelled"}:
                try:
                    pending_effects = _effect_ledger_or_raise(run.effect_receipts_json)
                except DurableJobTransitionError:
                    pending_effects = []
                to_status, pending_reason = _effect_recovery_state(pending_effects)
                reason = "native_turn_physical_completion_unproven" if pending_reason == "unknown_external_effect" else pending_reason
                family_status, family_reason = await self.native_turn_family_recovery_state_in_session(db, run)
                if family_status == "cost_liability":
                    to_status, reason = family_status, family_reason
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
                if run.job_kind in {"forgejo_issue_title_v1", "inference.near-text.v1", "browser_interact_v2", "goal_public_discovery_v1"} and terminal_authority_check is None:
                    raise DurableJobTransitionError("Forgejo terminalization requires its fixed native authority callback")
                if run.job_kind == "guardian_opportunity_assess" and terminal_authority_check is None:
                    raise DurableJobTransitionError("Opportunity terminalization requires its fixed native authority callback")
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
            report_next_journal = None
            if report_terminal:
                if to_status not in {"succeeded", "cancelled"}:
                    raise DurableJobTransitionError("original report outcome remains unresolved")
                from src.runtime_plugins.task_capability import prepare_report_terminal_publication, preflight_report_journal
                report_next_journal = await prepare_report_terminal_publication(
                    db, run, terminal=_native_report_terminal, to_status=to_status)
                if any(getattr(run, field) != value for field, value in report_original.items()):
                    raise DurableJobTransitionError("native report terminal preparation mutated original owner")
                preflight_report_journal(report_next_journal)
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
            if (to_status == "paused" and run.job_kind == "agent.task.v1"
                and reason == "general_task_approval_required"):
                # This precontact wait is not an ordinary operator pause.
                # Retain its reason so generic resume cannot erase approval.
                values["failure_reason"] = reason
            if to_status == "paused" and run.job_kind in {"research_dossier", "readonly_research_child"}:
                from src.work_board.research_contracts import WAIT_SOURCES, WAIT_CHILDREN, PROMPT_READY
                permitted = {WAIT_SOURCES, WAIT_CHILDREN} if run.job_kind == "research_dossier" else {PROMPT_READY}
                checkpoint_id = "research:phase" if run.job_kind == "research_dossier" else "research:prompt-ready"
                matches = [item.get("payload") for item in _json_load(run.checkpoint_receipts_json, [])
                    if item.get("checkpoint_id") == checkpoint_id]
                if reason not in permitted or len(matches) != 1 or not isinstance(matches[0], dict):
                    raise DurableJobTransitionError("research pause requires its exact typed checkpoint")
                if run.job_kind == "research_dossier" and matches[0].get("phase") != reason:
                    raise DurableJobTransitionError("research pause phase differs from its checkpoint")
                values["failure_reason"] = reason
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
            if report_next_journal is not None:
                values["checkpoint_receipts_json"] = _canonical(report_next_journal)
            if approval_resume_record is not None:
                values["effect_receipts_json"] = _canonical(
                    _job_effect_ledger(run, [*(effect_ledger or []), approval_resume_record])
                )
            conditions = [WorkflowRunState.run_identity == job_id, WorkflowRunState.status == current]
            conditions.append(WorkflowRunState.revision == current_revision)
            if report_original is not None:
                conditions.extend(getattr(WorkflowRunState, field) == value
                    for field, value in report_original.items())
                conditions.append(WorkflowRunState.attempt_count == 1)
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
                await _verify_native_child_sql_scope(db, run)
                _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
            if run.job_kind == "runtime_service_memory_v1" and to_status == "queued" and header_budget is not None:
                result_update = await db.execute(update(WorkflowRunState).execution_options(
                    synchronize_session=False).where(*conditions).values(**values))
            else:
                result_update = await _execute_original_memory_negative(self, db, run,
                    conditions, values, receipt={"kind": "transition", "status": "recorded",
                        "from": current, "to": to_status, "reason": _text(reason) or None,
                        "fencing_token": run.fencing_token, "revision": current_revision + 1,
                        "operator_visible": True})
            if not _rowcount_is_one(result_update):
                raise DurableJobLeaseError("durable job changed or lease fencing token is stale")
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                await header_budget.certify(db, WRS_BY_RUN, (job_id,))
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
        cancellation_authority_check: Callable[[Any, Any], Awaitable[None]] | None = None,
        result: Any = None,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        return await self.transition_job(
            job_id,
            "cancelled",
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
            reason=reason,
            cancellation_authority_check=cancellation_authority_check,
            result=result,
            result_summary=result_summary,
        )

    @_original_memory_maintenance_entry
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
        async with self._writer_session() as db:
            # Current admissions share ``root_run_identity``.  Older durable
            # rows can still be active with a self-root, however, so walk the
            # persisted parent links as a bounded frontier as well.  The
            # parent link is the exact tree edge; a different root's rows do
            # not enter this set merely because their root identity is close
            # in text or ordering.
            runs_by_id: dict[str, WorkflowRunState] = {}
            frontier = {root_job_id}
            while frontier:
                await _original_memory_maintenance_snapshot(self, db, writer=True)
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
            if current.get("github_capacity_closure"):
                receipts.append(dict(current))
                continue
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

    async def pause_general_task_for_approval(self, **bindings):
        """Commit the exact native no-contact wait in one canonical writer."""
        from src.work_board.general_task_approval import publish_approval_wait
        return await publish_approval_wait(self, **bindings)

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
        async with self._writer_session() as db:
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

    async def _record_turn_result_in_session(self, db, run, *, message,
                                             original_claim: NativeServiceClaim, authority_check):
        with db.no_autoflush:
            return await self._record_turn_result_unflushed(db, run, message=message,
                original_claim=original_claim, authority_check=authority_check)

    async def _validate_turn_completion_in_session(self, db, run, *,
                                                  original_claim: NativeServiceClaim, authority_check,
                                                  _family_phase="terminal"):
        """Original completion provenance; callers already hold no-autoflush."""
        from src.db.models import Message
        if (not db.in_transaction() or not db.info.get("native_writer_started")
            or not isinstance(original_claim, NativeServiceClaim)
            or authority_check is None or original_claim._host is None):
            raise DurableJobLeaseError("original native turn result writer required")
        host = original_claim._host
        if not host.admitting or host.boot_nonce != original_claim.host_boot_nonce:
            raise DurableJobLeaseError("original native turn host boot changed")
        receipt = _native_plain(original_claim.checkpoint)
        payload = receipt["payload"]
        history = _json_load(run.checkpoint_receipts_json, [])
        exact = [item for item in history if item.get("checkpoint_id") == receipt["checkpoint_id"]]
        if (exact != [receipt] or receipt.get("state_digest") != _digest(payload) or receipt.get("safe") is not True
            or run.job_kind != "conversation_turn_v1" or run.status != "running"
            or run.goal_id is not None or run.goal_revision is not None
            or run.run_identity != payload["invocation_ref"] or run.attempt_count != payload["attempt_count"]
            or run.input_digest != payload["input_digest"] or run.authority_digest != payload["authority_digest"]
            or run.run_fingerprint != payload["run_fingerprint"]
            or run.composition_binding_json != original_claim.binding.to_json()
            or int(_as_utc(run.deadline_at).timestamp() * 1000) != payload["original_deadline_at"]):
            raise DurableJobLeaseError("original native turn claim changed")
        self._assert_lease(run, owner=payload["lease_owner"], fencing_token=payload["fencing_token"])
        await _validate_native_service_claim(db, run,
            _NativeServiceClaimRequest(host, host.reviewed, original_claim.host_boot_nonce))
        await authority_check(db, run)
        now = _utc_now()
        if _deadline_expired(run, now=now):
            raise DurableJobLeaseError("original native turn deadline expired")
        arguments = _json_load(run.arguments_json, {})
        input_message = await db.get(Message, arguments.get("message_ref"))
        if (input_message is None or input_message.role != "user"
            or input_message.session_id != run.conversation_id
            or input_message.conversation_id != run.conversation_id
            or input_message.operator_session_id != run.operator_session_id
            or input_message.owner_principal_id != run.owner_principal_id
            or hashlib.sha256(input_message.content.encode()).hexdigest() != arguments.get("content_digest")):
            raise DurableJobLeaseError("original native turn input Message binding changed")
        if _family_phase == "terminal":
            from src.agent.turn_execution import NativeTurnExecution
            execution = getattr(authority_check, "__self__", None)
            if type(execution) is not NativeTurnExecution or execution.claim is not original_claim:
                raise DurableJobLeaseError("original native turn family execution required")
            await self.assert_native_turn_family_terminal_in_session(db, run, native_execution=execution)
        elif _family_phase not in {"initialize", "append"}:
            raise DurableJobLeaseError("original native turn family phase unsupported")
        return host, payload, history, input_message, now

    async def initialize_native_turn_family_in_session(self, db, *, native_execution):
        from src.agent.turn_execution import NativeTurnExecution
        from src.agent.native_turn_family import (FAMILY_CHECKPOINT_ID, build_initial_family_payload,
            validate_family_binding, validate_family_transition)
        if type(native_execution) is not NativeTurnExecution:
            raise DurableJobLeaseError("original native turn family execution required")
        if db.info.get("composition_guard") is None:
            raise DurableJobLeaseError("original native turn family writer unavailable")
        if not db.in_transaction():
            await _begin_legacy_aware_writer(db)
            db.info["native_writer_started"] = True
        with db.no_autoflush:
            run = await self._fetch(db, native_execution.admission.job_id)
            _, claim, history, _, now = await self._validate_turn_completion_in_session(db, run,
                original_claim=native_execution.claim, authority_check=native_execution.authority_check,
                _family_phase="initialize")
            if any(item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID for item in history):
                raise DurableJobLeaseError("original native turn family already initialized")
            payload = build_initial_family_payload(native_execution)
            validate_family_binding(payload, run, claim_payload=claim)
            validate_family_transition(None, payload)
            receipt = {"checkpoint_id": FAMILY_CHECKPOINT_ID, "state_digest": _digest(payload), "safe": True, "payload": payload}
            db.info["composition_native_turn_family_receipt"] = receipt
            from src.agent.native_turn_controls import reserve_control_capacity
            reserved = reserve_control_capacity(db, native_execution, [*history, receipt])
            run.checkpoint_receipts_json = _canonical(reserved)
            run.revision += 1
            run.updated_at = now
        await db.flush()
        if not native_execution.host.admitting or native_execution.host.boot_nonce != native_execution.claim.host_boot_nonce:
            raise DurableJobLeaseError("original native turn family host changed")

    async def seal_native_inference_output(self, *, result_witness, route_witness):
        """Only exact original inference/route witnesses can publish output."""
        from src.runtime_plugins.inference_output import seal_output
        return await seal_output(self, result_witness=result_witness, route_witness=route_witness)

    async def consume_native_inference_candidate_in_session(self, db, *, candidate):
        from src.runtime_plugins.inference_output import consume_candidate
        return await consume_candidate(self, db, candidate=candidate)

    async def append_native_turn_family_in_session(self, db, *, native_execution,
                                                  operation_witness, operation_run, reservation):
        from src.agent.turn_execution import NativeTurnExecution
        from src.agent.native_turn_family import (FAMILY_CHECKPOINT_ID, NativeOperationWitness,
            validate_family_binding, validate_family_transition, operation_payload_from_witness)
        if (type(native_execution) is not NativeTurnExecution or type(operation_witness) is not NativeOperationWitness
            or operation_witness.execution is not native_execution):
            raise DurableJobLeaseError("original native turn family producer required")
        from sqlalchemy import inspect
        if (inspect(operation_run).session is not db.sync_session or inspect(reservation).session is not db.sync_session):
            raise DurableJobLeaseError("original native turn family owner writer required")
        with db.no_autoflush:
            run = await self._fetch(db, native_execution.admission.job_id)
            _, claim, history, _, now = await self._validate_turn_completion_in_session(db, run,
                original_claim=native_execution.claim, authority_check=native_execution.authority_check,
                _family_phase="append")
            selected = [item for item in history if item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID]
            if len(selected) != 1 or selected[0].get("safe") is not True or selected[0].get("state_digest") != _digest(selected[0].get("payload")):
                raise DurableJobLeaseError("original native turn family receipt missing")
            previous = selected[0]["payload"]
            validate_family_binding(previous, run, claim_payload=claim)
            operation = operation_payload_from_witness(operation_witness, operation_run, reservation)
            payload = {**previous, "operations": [*previous["operations"], operation]}
            validate_family_transition(previous, payload)
            receipt = {"checkpoint_id": FAMILY_CHECKPOINT_ID, "state_digest": _digest(payload), "safe": True, "payload": payload}
            db.info["composition_native_turn_family_receipt"] = receipt
            run.checkpoint_receipts_json = _canonical([receipt if item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID else item for item in history])
            run.revision += 1
            run.updated_at = now
        await db.flush()
        if not native_execution.host.admitting or native_execution.host.boot_nonce != native_execution.claim.host_boot_nonce:
            raise DurableJobLeaseError("original native turn family host changed")

    async def assert_native_turn_family_terminal_in_session(self, db, run, *, native_execution=None):
        from src.agent.native_turn_family import FAMILY_CHECKPOINT_ID, assert_family_owner_readback
        entries = [item for item in _json_load(run.checkpoint_receipts_json, [])
            if isinstance(item, dict) and item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID]
        if (len(entries) != 1 or entries[0].get("safe") is not True
            or entries[0].get("state_digest") != _digest(entries[0].get("payload"))):
            raise DurableJobLeaseError("original native turn family receipt missing")
        await assert_family_owner_readback(self, db, run, entries[0]["payload"], native_execution=native_execution)
        effects = _effect_ledger_or_raise(run.effect_receipts_json)
        if _job_has_unsafe_effects(effects):
            raise DurableJobLeaseError("native_turn_family_unknown_external_effect")

    async def native_turn_family_recovery_state_in_session(self, db, run):
        """Project actual owner debt without transferring or settling it."""
        from src.agent.native_turn_family import FAMILY_CHECKPOINT_ID, validate_family_binding, validate_family_operation_reference
        from src.db.models import InferenceCostReservation
        entries = [item for item in _json_load(run.checkpoint_receipts_json, [])
            if isinstance(item, dict) and item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID]
        if not entries:
            return "unknown_external_effect", "native_turn_physical_completion_unproven"
        if len(entries) != 1 or entries[0].get("safe") is not True or entries[0].get("state_digest") != _digest(entries[0].get("payload")):
            raise DurableJobLeaseError("original native turn family receipt changed")
        payload = validate_family_binding(entries[0]["payload"], run)
        for operation in payload["operations"]:
            reservation = await db.get(InferenceCostReservation, operation["operation_id"], populate_existing=True)
            if reservation is None:
                raise DurableJobLeaseError("original native turn accounting owner missing")
            owner_run = await self._fetch(db, operation["job_id"])
            validate_family_operation_reference(operation, owner_run, reservation)
            if (reservation.state in {"contact_started", "unknown"} or owner_run.status == "cost_liability"
                or (reservation.state == "settled" and reservation.actual_cost_microusd is not None
                    and reservation.actual_cost_microusd > reservation.bound_microusd)):
                return "cost_liability", "native_turn_family_cost_liability"
        return "unknown_external_effect", "native_turn_physical_completion_unproven"

    async def _record_turn_result_unflushed(self, db, run, *, message,
                                           original_claim: NativeServiceClaim, authority_check):
        """Actual Message and protected result settle in the original writer."""
        from src.db.models import Message
        host, payload, history, input_message, now = await self._validate_turn_completion_in_session(
            db, run, original_claim=original_claim, authority_check=authority_check)
        if (not isinstance(message, Message) or message.role != "assistant"
            or message.id == input_message.id or message not in db.new
            or message.id != uuid5(NAMESPACE_URL,
                f"seraph-chat:{run.owner_principal_id}:{run.conversation_id}:{input_message.id}:assistant").hex
            or message.owner_principal_id != run.owner_principal_id
            or message.operator_session_id != run.operator_session_id
            or message.session_id != run.conversation_id or message.conversation_id != run.conversation_id
            or message.attachment_refs_json not in {None, "[]"}
            or len(message.content.encode()) > 65536):
            raise DurableJobLeaseError("original native turn output Message binding changed")
        result_payload = {"schema_version": 1, "message_ref": message.id,
            "input_message_ref": input_message.id, "no_learning": True}
        result_receipt = {"checkpoint_id": "conversation:assistant-message", "state_digest": _digest(result_payload),
            "safe": True, "payload": result_payload}
        if any(item.get("checkpoint_id") == result_receipt["checkpoint_id"] for item in history):
            raise DurableJobLeaseError("original native turn output already settled")
        db.info["composition_native_turn_receipt"] = result_receipt
        conditions = [WorkflowRunState.run_identity == run.run_identity,
            WorkflowRunState.status == "running", WorkflowRunState.revision == _revision(run),
            WorkflowRunState.lease_owner == payload["lease_owner"], WorkflowRunState.fencing_token == payload["fencing_token"],
            WorkflowRunState.attempt_count == payload["attempt_count"], WorkflowRunState.deadline_at > now,
            WorkflowRunState.lease_expires_at > now]
        _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
        result = await db.execute(update(WorkflowRunState).where(*conditions)
            .execution_options(synchronize_session=False).values(status="succeeded",
                checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts([*history, result_receipt])),
                result_digest=hashlib.sha256(message.content.encode()).hexdigest(), result_summary="Native turn response persisted",
                lease_owner=None, lease_expires_at=None, finished_at=now, updated_at=now, heartbeat_at=now,
                revision=WorkflowRunState.revision + 1))
        if not _rowcount_is_one(result):
            raise DurableJobLeaseError("original native turn changed before result settlement")
        if not host.admitting or host.boot_nonce != original_claim.host_boot_nonce:
            raise DurableJobLeaseError("original native turn host boot changed")
        await db.flush()
        settled = await self._fetch(db, run.run_identity)
        return _serialize(settled, receipt={"kind": "native_turn_result", "status": "succeeded", "no_learning": True})

    async def _record_turn_controlled_in_session(self, db, run, *, native_execution,
                                                completed_exception, authority_check, message=None):
        """Only the completed original callback can produce a controlled hold."""
        from src.agent.turn_execution import NativeTurnExecution
        from src.agent.controlled_origin import validate_controlled_origin
        from src.agent.exceptions import ClarificationRequired
        from src.approval.exceptions import ApprovalRequired
        from src.db.models import ApprovalRequest, Message
        if type(native_execution) is not NativeTurnExecution:
            raise DurableJobLeaseError("original native controlled execution required")
        native_execution.validate_completed_exception(completed_exception)
        try:
            origin = validate_controlled_origin(completed_exception, native_execution=native_execution)
        except (ValueError, PermissionError) as exc:
            raise DurableJobLeaseError("original canonical controlled producer required") from exc
        original_claim = native_execution.claim
        with db.no_autoflush:
            host, claim, history, input_message, now = await self._validate_turn_completion_in_session(
                db, run, original_claim=original_claim, authority_check=authority_check)
            if native_execution.admission.job_id != run.run_identity:
                raise DurableJobLeaseError("original controlled invocation changed")
            value = {"schema_version": 1, "input_message_ref": input_message.id, "no_learning": True}
            if type(completed_exception) is ClarificationRequired and origin.kind == "clarification":
                if (not isinstance(message, Message) or message not in db.new or message.role != "assistant"
                    or message.id != uuid5(NAMESPACE_URL,
                        f"seraph-chat:{run.owner_principal_id}:{run.conversation_id}:{input_message.id}:clarification").hex
                    or message.owner_principal_id != run.owner_principal_id
                    or message.operator_session_id != run.operator_session_id
                    or message.session_id != run.conversation_id or message.conversation_id != run.conversation_id
                    or message.attachment_refs_json not in {None, "[]"} or len(message.content.encode()) > 65536):
                    raise DurableJobLeaseError("original clarification Message binding changed")
                value.update(outcome="clarification_required", message_ref=message.id)
                status = "paused"
            elif type(completed_exception) is ApprovalRequired and origin.kind == "approval":
                snapshot = origin.approval_snapshot
                if (snapshot is None or completed_exception.approval_id != snapshot.approval_id
                    or completed_exception.tool_name != snapshot.tool_name):
                    raise DurableJobLeaseError("original approval producer snapshot changed")
                approval = await db.get(ApprovalRequest, snapshot.approval_id, populate_existing=True)
                if (message is not None or approval is None or approval.status != "pending"
                    or approval.resolved_at is not None or approval.expires_at is None
                    or _as_utc(approval.expires_at) <= now
                    or approval.owner_principal_id != run.owner_principal_id
                    or approval.operator_session_id != run.operator_session_id
                    or approval.conversation_id != run.conversation_id
                    or approval.tool_name != completed_exception.tool_name
                    or not _text(approval.tool_name) or not _text(approval.fingerprint)
                    or (approval.id, approval.tool_name, approval.fingerprint,
                        approval.owner_principal_id, approval.operator_session_id, approval.conversation_id,
                        _as_utc(approval.expires_at), _as_utc(approval.created_at)) !=
                       (snapshot.approval_id, snapshot.tool_name, snapshot.fingerprint,
                        snapshot.owner_principal_id, snapshot.operator_session_id, snapshot.conversation_id,
                        _as_utc(snapshot.expires_at), _as_utc(snapshot.created_at))):
                    raise DurableJobLeaseError("original pending approval reservation changed")
                value.update(outcome="approval_required", approval_ref=approval.id)
                status = "awaiting_approval"
            else:
                raise DurableJobLeaseError("original controlled exception required")
            identifier = "conversation:controlled-outcome"
            if any(item.get("checkpoint_id") in {identifier, "conversation:assistant-message"} for item in history):
                raise DurableJobLeaseError("original native turn already settled")
            effects = _effect_ledger_or_raise(run.effect_receipts_json)
            if _job_has_unsafe_effects(effects):
                status, reason = _effect_recovery_state(effects)
            else:
                reason = value["outcome"]
            receipt = {"checkpoint_id": identifier, "state_digest": _digest(value), "safe": True, "payload": value}
            db.info["composition_native_turn_receipt"] = receipt
            conditions = [WorkflowRunState.run_identity == run.run_identity,
                WorkflowRunState.status == "running", WorkflowRunState.revision == _revision(run),
                WorkflowRunState.lease_owner == claim["lease_owner"], WorkflowRunState.fencing_token == claim["fencing_token"],
                WorkflowRunState.attempt_count == claim["attempt_count"], WorkflowRunState.deadline_at > now,
                WorkflowRunState.lease_expires_at > now]
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
            result = await db.execute(update(WorkflowRunState).where(*conditions)
                .execution_options(synchronize_session=False).values(status=status,
                    checkpoint_receipts_json=_canonical(_bounded_checkpoint_receipts([*history, receipt])),
                    failure_reason=reason, result_summary="Native turn requires operator input",
                    lease_owner=None, lease_expires_at=None, updated_at=now, heartbeat_at=now,
                    revision=WorkflowRunState.revision + 1))
            if not _rowcount_is_one(result):
                raise DurableJobLeaseError("original native turn changed before controlled settlement")
            native_execution.validate_completed_exception(completed_exception)
            try:
                if validate_controlled_origin(completed_exception, native_execution=native_execution) is not origin:
                    raise DurableJobLeaseError("original canonical controlled producer changed")
            except (ValueError, PermissionError) as exc:
                raise DurableJobLeaseError("original canonical controlled producer changed") from exc
            if not host.admitting or host.boot_nonce != original_claim.host_boot_nonce:
                raise DurableJobLeaseError("original native turn host boot changed")
            await db.flush()
            settled = await self._fetch(db, run.run_identity)
            return _serialize(settled, receipt={"kind": "native_turn_controlled", "status": status, "no_learning": True})

    async def complete_native_read(self, original_claim: NativeServiceClaim, *, result) -> dict[str, Any]:
        """Settle the actual sealed native result, never adopt child echo as truth."""
        from src.runtime_plugins.read_journal import read_context
        if type(original_claim) is not NativeServiceClaim or original_claim._host is None:
            raise DurableJobLeaseError("original native read claim required")
        host = original_claim._host
        async with self._writer_session() as db:
            if db.info.get("composition_guard") is None or db.in_transaction():
                raise DurableJobLeaseError("original native read completion writer unavailable")
            await _begin_legacy_aware_writer(db)
            db.info["native_writer_started"] = True
            run = await self._fetch(db, original_claim.job["job_id"])
            context = read_context(run)
            receipt = _native_plain(original_claim.checkpoint)
            payload = receipt["payload"]
            if (run.status != "running" or context["result"] is None
                or context["result"]["payload"] != result
                or context["result"]["claim_ref"] != payload["claim_ref"]
                or run.composition_binding_json != original_claim.binding.to_json()
                or receipt not in _json_load(run.checkpoint_receipts_json, [])
                or run.input_digest != payload["input_digest"] or run.authority_digest != payload["authority_digest"]
                or run.run_fingerprint != payload["run_fingerprint"]
                or run.attempt_count != payload["attempt_count"] or _deadline_expired(run, now=_utc_now())):
                raise DurableJobLeaseError("original native read completion changed")
            self._assert_lease(run, owner=payload["lease_owner"], fencing_token=payload["fencing_token"])
            await _validate_native_service_claim(db, run,
                _NativeServiceClaimRequest(host, host.reviewed, original_claim.host_boot_nonce))
            if "artifact_profile" in context:
                from src.runtime_plugins.read_journal import assert_artifact_completion
                await assert_artifact_completion(db, run)
            now = _utc_now()
            run.status, run.result_digest, run.result_summary = "succeeded", context["result"]["result_digest"], "Native metadata read completed; no_learning"
            run.finished_at, run.updated_at = now, now
            run.lease_owner, run.lease_expires_at = None, None
            run.revision += 1
            await db.flush()
            if not host.admitting or host.boot_nonce != original_claim.host_boot_nonce:
                raise DurableJobLeaseError("original native read host changed before settlement")
            return _serialize(run, receipt={"kind": "native_read_completed", "no_learning": True})

    async def complete_native_memory(self, original_claim: NativeServiceClaim, *, result, header_budget=None) -> dict[str, Any]:
        """Complete only the same-writer actual Memory result and retained owners."""
        from src.runtime_plugins.memory_producer import memory_context
        from src.runtime_plugins.ownership import begin_native_writer
        from src.workspace.accounting_witness import (
            validate_native_memory_retention,
            prepare_native_memory_lifecycle,
            apply_native_memory_lifecycle,
        )
        if type(original_claim) is not NativeServiceClaim or original_claim._host is None:
            raise DurableJobLeaseError("original native Memory claim required")
        host = original_claim._host
        async with self._writer_session(**(
            {"header_budget": header_budget} if header_budget is not None else {})) as db:
            await begin_native_writer(db, owner="durable_jobs", **(
                {"header_budget": header_budget} if header_budget is not None else {}))
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                await header_budget.certify(db, WRS_BY_RUN, (original_claim.job["job_id"],))
            run = await self._fetch(db, original_claim.job["job_id"])
            context = memory_context(run)
            sealed = context["result"]
            receipt = _native_plain(original_claim.checkpoint)
            payload = receipt["payload"]
            if (run.status != "running" or sealed is None or sealed["payload"] != result
                or sealed["claim_ref"] != payload["claim_ref"]
                or run.composition_binding_json != original_claim.binding.to_json()
                or receipt not in _json_load(run.checkpoint_receipts_json, [])
                or run.input_digest != payload["input_digest"] or run.authority_digest != payload["authority_digest"]
                or run.run_fingerprint != payload["run_fingerprint"] or run.attempt_count != payload["attempt_count"]
                or _deadline_expired(run, now=_utc_now())):
                raise DurableJobLeaseError("original native Memory completion changed")
            self._assert_lease(run, owner=payload["lease_owner"], fencing_token=payload["fencing_token"])
            await _validate_native_service_claim(db, run,
                _NativeServiceClaimRequest(host, host.reviewed, original_claim.host_boot_nonce, header_budget=header_budget))
            from src.memory.header_bounds import GOAL
            if run.goal_id is not None:
                await header_budget.certify(db, GOAL, (run.goal_id,))
            await _assert_canonical_goal_fence(db, goal_id=run.goal_id,
                goal_revision=run.goal_revision, owner_kind=run.owner_kind,
                owner_principal_id=run.owner_principal_id, session_id=run.session_id,
                authority=run.declared_authority_json)
            await validate_native_memory_retention(db, run, sealed)
            now = _utc_now()
            # Stage the original owner's whole causal patch before computing
            # the self-containing Current envelope. The private preparation
            # binds the actual registered claim, writer, prior row and journal;
            # these values alone carry no publication authority.
            pending_changes = {
                "status": "succeeded" if result["status"] == "succeeded" else "blocked",
                "result_digest": sealed["result_digest"],
                "result_summary": "Native Memory owner result committed",
                "failure_reason": result.get("reason_code"),
                "finished_at": now,
                "updated_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "revision": _revision(run) + 1,
                "checkpoint_context_json": run.checkpoint_context_json,
            }
            lifecycle = await prepare_native_memory_lifecycle(db, run,
                original_claim=original_claim, pending_changes=pending_changes,
                header_budget=header_budget)
            if not host.admitting or host.boot_nonce != original_claim.host_boot_nonce:
                raise DurableJobLeaseError("original native Memory host changed before settlement")
            await apply_native_memory_lifecycle(db, run, lifecycle)
            return _serialize(run, receipt={"kind": "native_memory_completed", "memory_status": result["memory_status"]})

    async def claim_service_job(self, job_id: str, *, host, owner: str,
                                lease_seconds: int = 300, expected_revision: int | None = None,
                                expected_fencing_token: int | None = None, claim_authority_check=None,
                                native_report_candidate=None, header_budget=None,
                                native_memory_admission=None) -> NativeServiceClaim:
        from src.runtime_plugins.bridge import CordisHost
        from src.runtime_plugins.composition import ReviewedComposition
        if (not isinstance(host, CordisHost) or not host.admitting
                or not isinstance(host.reviewed, ReviewedComposition)
                or type(host.boot_nonce) is not str or not re.fullmatch(r"[0-9a-f]{64}", host.boot_nonce)):
            raise DurableJobLeaseError("admitting original reviewed native service host required")
        request = _NativeServiceClaimRequest(host, host.reviewed, host.boot_nonce, native_report_candidate, header_budget, native_memory_admission)
        result = await self.claim_job(job_id, owner=owner, lease_seconds=lease_seconds,
            expected_revision=expected_revision, expected_fencing_token=expected_fencing_token,
            claim_authority_check=claim_authority_check, _runtime_service_claim=request)
        if not isinstance(result, NativeServiceClaim):
            raise DurableJobLeaseError("native service claim did not acquire an original running attempt")
        return result

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
        claim_authority_check=None,
        opportunity_preference_witness=None,
        _runtime_service_claim: _NativeServiceClaimRequest | None = None,
    ) -> dict[str, Any] | NativeServiceClaim:
        owner = _text(owner)
        if not owner:
            raise DurableJobLeaseError("owner is required to claim a job")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        if expected_state != "queued":
            raise DurableJobTransitionError("durable job claims require the queued state")
        header_budget = _runtime_service_claim.header_budget if _runtime_service_claim is not None else None
        async with self._writer_session(**(
            {"header_budget": header_budget} if header_budget is not None else {})) as db:
            from src.memory.evidence_dependencies import stage_run_dependencies, recheck_run_dependencies
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                await header_budget.certify(db, WRS_BY_RUN, (job_id,))
            preflight_run = await self._fetch(db, job_id)
            if preflight_run.job_kind in {"forgejo_issue_title_v1", "inference.near-text.v1", "browser_interact_v2", "goal_public_discovery_v1"} and claim_authority_check is None:
                raise DurableJobLeaseError("Forgejo claims require the fixed native authority callback")
            if preflight_run.job_kind == "guardian_opportunity_assess" and claim_authority_check is None:
                raise DurableJobLeaseError("Opportunity claims require the fixed native authority callback")
            if str(preflight_run.status) in DURABLE_JOB_TERMINAL_STATUSES:
                await _assert_canonical_goal_fence(db, goal_id=preflight_run.goal_id,
                    goal_revision=preflight_run.goal_revision, owner_kind=preflight_run.owner_kind,
                    owner_principal_id=preflight_run.owner_principal_id, session_id=preflight_run.session_id,
                    authority=preflight_run.declared_authority_json)
                db.expunge(preflight_run)
                return _serialize(preflight_run, receipt={'kind': 'claim', 'status': 'terminal_noop'})
            dependency_guard = preflight_run.job_kind in {'browser_public_task',
                'work.evidence-dossier.v1', 'work.local-evidence-report.v1'}
            preference_guard = preflight_run.job_kind == "memory.opportunity-preference.v1"
            staged_dependencies = await stage_run_dependencies(db, preflight_run) if dependency_guard else None
            await db.rollback()
            if _runtime_service_claim is not None:
                from src.runtime_plugins.ownership import begin_native_writer
                await begin_native_writer(db, owner="durable_jobs", **(
                    {"header_budget": header_budget} if header_budget is not None else {}))
            elif claim_authority_check is not None or dependency_guard or preference_guard:
                await _begin_legacy_aware_writer(db)
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                await header_budget.certify(db, WRS_BY_RUN, (job_id,))
            run = await self._fetch(db, job_id)
            if run.job_kind == "work.local-evidence-report.v1" and run.composition_binding_json is not None:
                if (_runtime_service_claim is None or continue_existing_attempt or run.attempt_count != 0):
                    raise DurableJobLeaseError("the original native report claim is exhausted or unavailable")
            service_binding = await _validate_native_service_claim(db, run, _runtime_service_claim) if _runtime_service_claim is not None else None
            if _native_turn_pending(run):
                raise DurableJobLeaseError("original native turn physical completion is unproven; replay denied")
            if preference_guard:
                from src.work_board.opportunity_preference_native import recheck_native
                await recheck_native(db,run,witness=opportunity_preference_witness)
                if run.attempt_count >= 1 or run.max_attempts != 1:
                    raise DurableJobLeaseError("the original recommendation attempt is exhausted")
            if run.job_kind == "general_task_native_tool_v1":
                from src.workflows.general_task_guard import assert_general_task_child_phase_current
                if continue_existing_attempt or run.attempt_count != 0 or run.fencing_token != 0:
                    raise DurableJobLeaseError("the original general task native claim is exhausted")
                await assert_general_task_child_phase_current(db, run)
                if json.loads(run.arguments_json).get("tool_id") == "document_build":
                    from src.work_board.document_build_native import validate_claim
                    validate_claim(self, run, claim_authority_check)
            if dependency_guard:
                await recheck_run_dependencies(db, run, staged_dependencies)
            if claim_authority_check is not None:
                if run.job_kind == "work_board_proposal":
                    from src.guardian.opportunity_plans import assert_linked_plan_native
                    await assert_linked_plan_native(db, run)
                elif run.job_kind == "general_task_native_tool_v1":
                    from src.work_board.document_build_native import validate_claim
                    validate_claim(self, run, claim_authority_check)
                elif run.job_kind not in {"readonly_research_child", "document_invoice_compare_v1", "local_authored_json", "forgejo_issue_title_v1", "guardian_opportunity_assess", "inference.near-text.v1", "browser_interact_v2", "goal_public_discovery_v1"} and not (_runtime_service_claim is not None and run.job_kind in {"workflow", "conversation_turn_v1", "research_dossier", "runtime_service_read_v1", "runtime_service_memory_v1", "work.local-evidence-report.v1"}):
                    raise DurableJobLeaseError("phase-bound claims require a fixed native capability")
                await claim_authority_check(db, run)
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
                if run.job_kind == "runtime_service_memory_v1":
                    raise DurableJobLeaseError("original_memory_negative_claim_unavailable")
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
                await _verify_native_child_sql_scope(db, run)
                _append_parent_fence_condition(deadline_conditions, run, now=now, writer_db=db)
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
                if header_budget is not None:
                    from src.memory.header_bounds import WRS_BY_RUN
                    await header_budget.certify(db, WRS_BY_RUN, (job_id,))
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
                if run.job_kind == "runtime_service_memory_v1":
                    raise DurableJobLeaseError("original_memory_negative_claim_unavailable")
                malformed_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                    or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= now),
                ]
                await _verify_native_child_sql_scope(db, run)
                _append_parent_fence_condition(malformed_conditions, run, now=now, writer_db=db)
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
                if header_budget is not None:
                    from src.memory.header_bounds import WRS_BY_RUN
                    await header_budget.certify(db, WRS_BY_RUN, (job_id,))
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
            research_resume = False
            if (run.job_kind == "readonly_research_child" and continue_existing_attempt
                and claim_authority_check is not None):
                from src.work_board.research_control import precontact_intent_reusable
                research_resume = await precontact_intent_reusable(self, db, run, effect_ledger)
            if _job_has_unsafe_effects(effect_ledger) and not research_resume:
                if run.job_kind == "runtime_service_memory_v1":
                    raise DurableJobLeaseError("original_memory_negative_claim_unavailable")
                recovery_status, recovery_reason = _effect_recovery_state(effect_ledger)
                recovery_reason = f"queued_{recovery_reason}_requires_reconciliation"
                recovery_conditions = [
                    WorkflowRunState.run_identity == job_id,
                    WorkflowRunState.status == expected_state,
                    WorkflowRunState.revision == current_revision,
                    WorkflowRunState.fencing_token == expected_fence,
                    or_(WorkflowRunState.lease_owner.is_(None), WorkflowRunState.lease_expires_at <= now),
                ]
                await _verify_native_child_sql_scope(db, run)
                _append_parent_fence_condition(recovery_conditions, run, now=now, writer_db=db)
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
                if header_budget is not None:
                    from src.memory.header_bounds import WRS_BY_RUN
                    await header_budget.certify(db, WRS_BY_RUN, (job_id,))
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
                if run.job_kind == "runtime_service_memory_v1":
                    raise DurableJobLeaseError("original_memory_negative_claim_unavailable")
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
                await _verify_native_child_sql_scope(db, run)
                _append_parent_fence_condition(dependency_failure_conditions, run, now=now, writer_db=db)
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
                if header_budget is not None:
                    from src.memory.header_bounds import WRS_BY_RUN
                    await header_budget.certify(db, WRS_BY_RUN, (job_id,))
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
                if run.job_kind == "runtime_service_memory_v1":
                    raise DurableJobLeaseError("original_memory_negative_claim_unavailable")
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
                await _verify_native_child_sql_scope(db, run)
                _append_parent_fence_condition(dependency_block_conditions, run, now=now, writer_db=db)
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
                if header_budget is not None:
                    from src.memory.header_bounds import WRS_BY_RUN
                    await header_budget.certify(db, WRS_BY_RUN, (job_id,))
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
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
            service_checkpoint = None
            if _runtime_service_claim is not None:
                _runtime_service_claim.validate_host()
                if continue_existing_attempt or accounting_resume or research_resume:
                    raise DurableJobLeaseError("native service claim cannot renew an existing attempt")
                attempt, fence = int(run.attempt_count) + 1, expected_fence + 1
                claim_ref = _digest({"job_id": run.run_identity, "attempt_count": attempt, "fencing_token": fence})
                checkpoint_id = _RUNTIME_SERVICE_CLAIM_PREFIX + claim_ref
                payload = {"schema_version": 2, "invocation_ref": run.run_identity, "claim_ref": claim_ref,
                    "checkpoint_id": checkpoint_id, "origin_method": service_binding.origin_method,
                    "native_branch": service_binding.native_branch,
                    "allowed_child_methods": list(service_binding.allowed_child_methods),
                    "method_manifest_version": service_binding.payload()["method_manifest_version"],
                    "method_manifest_digest": service_binding.payload()["method_manifest_digest"],
                    "attempt_count": attempt, "lease_owner": owner, "fencing_token": fence,
                    "input_digest": run.input_digest, "authority_digest": run.authority_digest,
                    "run_fingerprint": run.run_fingerprint,
                    "original_deadline_at": int(persisted_deadline.timestamp() * 1000),
                    "package_digest": service_binding.host_package_digest,
                    "host_composition_digest": service_binding.host_composition_digest,
                    "composition_binding_digest": service_binding.binding_digest,
                    "host_boot_nonce": _runtime_service_claim.host_boot_nonce}
                service_checkpoint = {"checkpoint_id": checkpoint_id, "state_digest": _digest(payload),
                    "safe": True, "payload": payload}
                history = _json_load(run.checkpoint_receipts_json, [])
                if any(item.get("checkpoint_id") == checkpoint_id for item in history if isinstance(item, Mapping)):
                    raise DurableJobLeaseError("native service claim already stamped")
                claim_values["checkpoint_receipts_json"] = _canonical(_bounded_checkpoint_receipts([*history, service_checkpoint]))
                db.info["composition_native_claim_receipt"] = service_checkpoint
            if run.job_kind == "runtime_service_memory_v1":
                from src.runtime_plugins.memory_producer import NativeMemoryMutationAdmission, memory_context
                from src.workspace.accounting_witness import (
                    preflight_native_memory_reference_journal,
                    reserve_native_memory_pending_run,
                )
                if (_runtime_service_claim is None or service_checkpoint is None
                        or type(_runtime_service_claim.native_memory_admission) is not NativeMemoryMutationAdmission
                        or _runtime_service_claim.native_memory_admission.header_budget is not header_budget
                        or memory_context(run)["result"] is not None
                        or preflight_native_memory_reference_journal(run.checkpoint_receipts_json) != (None, None)):
                    raise DurableJobLeaseError("native_memory_original_claim_reserve_unavailable")
                # The unchanged original revision/fence CAS binds these exact
                # concrete values. Ordinary claims retain SQL arithmetic.
                claim_values["fencing_token"] = expected_fence + 1
                claim_values["revision"] = current_revision + 1
                claim_values["attempt_count"] = int(run.attempt_count) + 1
                pending_receipt = {
                    "kind": "claim", "status": "claimed", "owner": owner,
                    "fencing_token": expected_fence + 1,
                    "lease_expires_at": expires.isoformat(),
                    "attempt": int(run.attempt_count) + 1,
                    "revision": current_revision + 1, "operator_visible": True,
                }
                output = _native_memory_pending_output(run, claim_values, receipt=pending_receipt)
                await reserve_native_memory_pending_run(db, run, claim_values, header_budget,
                    outputs=(output, service_checkpoint))
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(**claim_values)
            )
            if not _rowcount_is_one(result_update):
                raise DurableJobLeaseError("job is currently owned by another active runner")
            if header_budget is not None:
                from src.memory.header_bounds import WRS_BY_RUN
                await header_budget.certify(db, WRS_BY_RUN, (job_id,))
            claimed = await self._fetch(db, job_id)
            if _runtime_service_claim is not None:
                _runtime_service_claim.validate_host()
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
            if service_checkpoint is not None:
                return NativeServiceClaim(_frozen_native_value(_serialize(claimed, receipt=receipt)),
                    _frozen_native_value(service_checkpoint), service_binding, _runtime_service_claim.host_boot_nonce,
                    _runtime_service_claim.host)
            return _serialize(claimed, receipt=receipt)

    @_original_memory_maintenance_entry
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
        async with self._writer_session() as db:
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
                await _verify_native_child_sql_scope(db, run)
                _append_parent_fence_condition(deadline_conditions, run, now=now, writer_db=db)
                expired = await _execute_original_memory_negative(self, db, run,
                    deadline_conditions, {"status": "failed", "failure_reason": "deadline_expired",
                        "lease_owner": None, "lease_expires_at": None, "finished_at": now,
                        "updated_at": now, "heartbeat_at": now, "revision": current_revision + 1},
                    receipt={"kind": "heartbeat", "status": "failed", "reason": "deadline_expired",
                        "owner": owner, "fencing_token": run.fencing_token,
                        "revision": current_revision + 1, "operator_visible": True})
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
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_positive_heartbeat_unavailable")
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(heartbeat_conditions, run, now=now, writer_db=db)
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

    @_deny_original_memory_generic_entry
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
        async with self._writer_session() as db:
            run = await self._fetch(db, job_id)
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(transfer_conditions, run, now=now, writer_db=db)
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

    @_deny_original_memory_generic_entry
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
        opportunity_preference_witness=None,
        native_physical_reservation=None,
    ) -> dict[str, Any]:
        if checkpoint_id == "general-task:current-manifest:v1":
            raise DurableJobTransitionError("general task manifest requires its fixed native writer")
        if isinstance(checkpoint_id, str) and checkpoint_id.startswith("general:delegation:"):
            raise DurableJobTransitionError("specialist delegation requires its fixed reservation writer")
        if isinstance(checkpoint_id, str) and checkpoint_id.startswith(("general:approval:", "general:cleanup:", "general:cancel:")):
            raise DurableJobTransitionError("native transition and callback closure require their fixed writer")
        if checkpoint_id == "native-physical-resource-cleanup":
            raise DurableJobTransitionError("native cleanup requires its fixed resource owner")
        if checkpoint_id in {"document-capacity", "document-child", "document-reaped"}:
            raise DurableJobTransitionError("document process reservation/reap requires its fixed native owner")
        if not _text(checkpoint_id):
            raise ValueError("checkpoint_id is required")
        if _protected_composition_checkpoint(checkpoint_id):
            raise DurableJobTransitionError("protected native claim writer required")
        async with self._writer_session() as db:
            from src.memory.evidence_dependencies import stage_run_dependencies, recheck_run_dependencies
            preflight_run = await self._fetch(db, job_id)
            staged_dependencies = await stage_run_dependencies(db, preflight_run)
            await db.rollback()
            # SQLite WAL readers cannot reliably upgrade a snapshot to a
            # writer while another dispatcher is committing.  Acquire the
            # same immediate writer boundary used by durable admission and
            # repair-capacity reservation before reading the row, preserving
            # the existing fenced CAS below without masking a real conflict.
            bind = db.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", "") == "sqlite":
                await _begin_legacy_aware_writer(db)
            run = await self._fetch(db, job_id)
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")
            if run.job_kind == "memory.opportunity-preference.v1" and checkpoint_id == "opportunity-preference-source-use":
                from src.work_board.opportunity_preference_native import recheck_native
                await recheck_native(db,run,witness=opportunity_preference_witness)
            from src.workflows.general_task_guard import requires_native_writer, verify_native_writer
            if requires_native_writer(run):
                await verify_native_writer(self, db, run)
            await recheck_run_dependencies(db, run, staged_dependencies)
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
                if checkpoint_id == "native-physical-resource-reservation":
                    from src.db.models import OperatorSession
                    if (not isinstance(native_physical_reservation, tuple) or len(native_physical_reservation) != 3):
                        raise DurableJobTransitionError("native typed physical reservation is required")
                    binding, authenticated_owner, token_hash = native_physical_reservation
                    if type(binding) is not NativePhysicalCleanupBinding:
                        raise DurableJobTransitionError("native typed physical binding is required")
                    claims = _json_load(run.resource_claims_json, [])
                    if (run.job_kind not in {"connection_source_sync", "browser_interact_v2"}
                        or run.capability_version != {"connection_source_sync":"connection-sync-v1", "browser_interact_v2":"2"}[run.job_kind]
                        or run.owner_kind != "user" or run.status != "running"
                        or run.operator_session_id != run.session_id or len(claims) != 1
                        or run.owner_principal_id != authenticated_owner.principal_id
                        or run.operator_session_id != authenticated_owner.session_id):
                        raise DurableJobTransitionError("native original physical execution changed")
                    root = await db.get(OperatorSession, authenticated_owner.session_id)
                    now = _utc_now()
                    if (root is None or root.principal_id != authenticated_owner.principal_id
                        or not token_hash or root.token_hash != token_hash or root.revoked_at is not None
                        or root.replaced_by_id is not None or root.is_bearer_tombstone
                        or _as_utc(root.idle_expires_at) <= now or _as_utc(root.absolute_expires_at) <= now):
                        raise DurableJobTransitionError("native original physical Root is inactive")
                    witness = checkpoint_payload.get("witness") if isinstance(checkpoint_payload, dict) else None
                    actual = NativePhysicalCleanupBinding(run.run_identity, _revision(run), run.owner_principal_id,
                        run.operator_session_id, run.session_id, run.input_digest, run.authority_digest,
                        run.run_fingerprint, run.attempt_count, run.lease_owner, run.fencing_token,
                        claims[0], _digest(witness))
                    if (binding != actual or checkpoint_payload != {"binding": native_physical_cleanup_binding_payload(binding), "witness": witness}
                        or state != checkpoint_payload
                        or any(item.get("checkpoint_id") == checkpoint_id for item in _json_load(run.checkpoint_receipts_json, []))):
                        raise DurableJobTransitionError("native original physical reservation cannot be replaced")
                    _native_physical_witness(run.job_kind, witness, binding)
                    receipt["payload"] = checkpoint_payload
                elif run.job_kind == "goal_public_discovery_v1" and checkpoint_id == "discovery:outcome":
                    from src.guardian.research_plan_contracts import DiscoveryOutcomeCheckpoint
                    typed = DiscoveryOutcomeCheckpoint.model_validate(checkpoint_payload)
                    from src.workflows.research_guard import current_discovery_witness
                    witness = current_discovery_witness()
                    artifact = witness.artifacts.get(typed.artifact_ref.artifact_id)
                    if (artifact is None or artifact["kind"] != "brief" or artifact["reference"] != typed.artifact_ref
                            or artifact["parsed"]["coverage"]["outcome_state"] != typed.state
                            or artifact["parsed"]["coverage"]["status"] != typed.coverage
                            or artifact["parsed"]["coverage"]["sources"] != checkpoint_payload["sources"]):
                        raise DurableJobTransitionError("programme final checkpoint differs from original physical brief")
                    receipt["payload"] = typed.model_dump(mode="json")
                else:
                    receipt["payload"] = _safe_structure(checkpoint_payload)
            elif checkpoint_id == "native-physical-resource-reservation":
                raise DurableJobTransitionError("native physical reservation must be safe and typed")
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(checkpoint_conditions, run, now=now, writer_db=db)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*checkpoint_conditions)
                .values(
                    checkpoint_receipts_json=_canonical(_github_recovery_history(run, existing, kind="checkpoint")),
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
        async with self._writer_session() as db:
            bind = db.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect_name == "sqlite":
                await _begin_legacy_aware_writer(db)
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
                    checkpoint_receipts_json=_canonical(_github_recovery_history(run, existing, kind="checkpoint")),
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
        async with self._writer_session() as db:
            bind = db.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", "") == "sqlite":
                await _begin_legacy_aware_writer(db)
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
                    checkpoint_receipts_json=_canonical(_github_recovery_history(run, existing, kind="checkpoint")),
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

    async def node_process_cleanup_projection(
        self, job_id: str, *, expected_revision: int, original_dispatch: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Expose recorded physical settlement, never execution/artifact success.

        This is a read-only historical projection. Live goal/approval expiry is
        deliberately not a reason to erase an already recorded cleanup fact.
        The canonical reservation parser and original dispatch binding remain
        the authority; arbitrary checkpoint flags are not public proof.
        """
        unverified = {"status": "unverified", "physical_capacity_released": False,
                      "cleanup_receipt_verified": False, "readback_scope": None}
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            declared = _json_load(run.declared_authority_json, {})
            if (run.job_kind != "engineering.repo-repair.v1" or run.status != "unknown_external_effect"
                or declared.get("sandbox_profile") != "repo-node24-npm-v1" or declared.get("executor_kind") != "local"
                or _revision(run) != expected_revision):
                return unverified
            try:
                reservation = self._repo_repair_reservation_state(run)
            except DurableJobTransitionError:
                return unverified
            dispatches = [item for item in _json_load(run.checkpoint_receipts_json, [])
                          if isinstance(item, dict) and isinstance(item.get("payload"), dict)
                          and item["payload"].get("phase") == "executor_dispatch_reserved"]
            if not dispatches:
                return unverified
            envelope = dispatches[-1]
            dispatch = {**envelope["payload"], "fencing_token": envelope.get("fencing_token")}
            fence = dispatch.get("fencing_token")
            attempt_id = _text(declared.get("attempt_id"))
            digest = _text(run.authority_digest)
            if (type(fence) is not int or fence <= 0 or fence > 2**53 - 1
                or not attempt_id or len(attempt_id) > 128 or "\x00" in attempt_id
                or len(job_id) > 128 or "\x00" in job_id or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or dispatch != dict(original_dispatch)
                or not self._repo_repair_reservation_matches(reservation, job_id=job_id, attempt_id=attempt_id,
                                                           fence=fence, authority_digest=digest)):
                return unverified
            if reservation.get("status") == "held":
                return {**unverified, "status": "held"}
            if reservation.get("readback_scope") != "process_cleanup_only":
                return unverified
            return {"status": "released", "physical_capacity_released": True, "cleanup_receipt_verified": True,
                    "readback_scope": "process_cleanup_only", "job_id": job_id, "attempt_id": attempt_id,
                    "fencing_token": fence, "authority_digest": digest,
                    "process_cleanup_readback_sha256": reservation["process_cleanup_readback_sha256"]}

    async def settle_node_process_cleanup(self, request: NodeProcessCleanupSettlement) -> dict[str, Any]:
        """Release only exact cancelled physical work; retain all task liability."""
        from config.settings import RepoSandboxSettings, settings
        from src.db.models import OperatorSession, RepoRepairProposal as RepoRepairProposalRow, RepoRepairSourcePacket as RepoRepairSourcePacketRow
        from src.execution.repo_node import NodeRepoRepairExecutor, PROFILE
        from src.workflows.repo_repair import _proposal_authority_payload, _authority_digest

        if type(request) is not NodeProcessCleanupSettlement:
            raise DurableJobTransitionError("Node cleanup settlement request is invalid")
        async with self._writer_session() as db:
            if db.get_bind().dialect.name == "sqlite":
                await _begin_legacy_aware_writer(db)
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
            if (_authority_digest(canonical) != proposal.authority_digest
                or any(request.authority.get(key) != value for key,value in canonical.items() if value not in (None,"",[],{}))
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
            if reservation is None or reservation.get("status") not in {"held","released"}:
                raise DurableJobTransitionError("Node physical reservation is missing")
            if reservation.get("status") == "released" and reservation.get("readback_scope") != "process_cleanup_only":
                raise DurableJobTransitionError("Node physical reservation has a different settlement")
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
                if reservation.get("status") == "released":
                    if reservation.get("process_cleanup_readback_sha256") != digest:
                        raise DurableJobTransitionError("Node settled cleanup readback changed")
                    db.expunge(run)
                    return _serialize(run, receipt={"kind":"repo_repair_process_cleanup_release","status":"deduped",
                                                   "readback_scope":"process_cleanup_only"})
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
                ).values(checkpoint_receipts_json=_canonical(_github_recovery_history(run, history, kind="checkpoint")),
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

        async with self._writer_session() as db:
            bind = db.get_bind()
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect_name == "sqlite":
                await _begin_legacy_aware_writer(db)
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

    @_deny_original_memory_generic_entry
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
        if _protected_composition_checkpoint(checkpoint_id):
            raise DurableJobTransitionError("protected native claim writer required")
        if not _text(owner_kind) or not _text(owner_principal_id):
            raise DurableJobLeaseError("recovery owner identity is required")
        async with self._writer_session() as db:
            # Recovery checkpoints also perform a read-modify-write of the
            # bounded checkpoint history.  Serialize that transaction on
            # SQLite before the fail-closed reservation validator runs.
            bind = db.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", "") == "sqlite":
                await _begin_legacy_aware_writer(db)
            run = await self._fetch(db, job_id)
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    checkpoint_receipts_json=_canonical(_github_recovery_history(run, existing, kind="checkpoint")),
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

    async def record_native_read_artifact_in_session(self, db, run, *, operation, call_ref,
                                                  file_path, content, trust_request, trust_decision):
        """Canonical governed artifact on the original live read's writer."""
        from src.runtime_plugins.read_journal import validate_operation, _artifact_writer, artifact_source_bytes
        from src.work_board.input_artifacts import _safe_file_bytes
        from src.workspace import canonical_workspace_root
        from config.settings import settings
        from pathlib import Path
        context = _artifact_writer(db, run)
        validate_operation(run, operation, call_ref=call_ref)
        candidate = operation.candidate()
        from src.runtime_plugins.read_artifacts import native_read_artifact_path, artifact_trust_request
        if candidate["method"] not in {"artifacts.stage", "artifacts.adopt", "artifacts.read"}:
            raise DurableJobLeaseError("original native artifact method required")
        expected = artifact_source_bytes(context)
        from src.auth.service import authenticate_principal
        principal = await authenticate_principal(run.owner_principal_id, db=db)
        expected_request = artifact_trust_request(run, content_digest=candidate["content_digest"],
            request_ref=candidate.get("request_ref") or candidate["artifact_ref"], principal=principal.principal)
        reference = Path(file_path)
        if (type(content) is not bytes or content != expected or reference.is_absolute()
            or file_path != native_read_artifact_path(run, candidate["content_digest"])
            or ".." in reference.parts or not file_path.startswith("artifacts/work-board/runtime-service-read/")
            or trust_request.principal.principal_id != run.owner_principal_id
            or trust_request.job_id != run.run_identity or trust_request.session_id != run.session_id
            or trust_request != expected_request
            or trust_request.data_digest != hashlib.sha256(expected).hexdigest()):
            raise DurableJobLeaseError("original native artifact source changed")
        _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / reference,
            expected_digest=candidate["content_digest"], expected_size=len(expected))
        record = build_artifact_record(file_path=file_path, artifact_type="native_service_read",
            producer=run.job_kind, run_id=run.run_identity, session_id=run.session_id, content=content,
            governed=True, trust_request=trust_request, trust_decision=trust_decision)
        receipt = {key: record[key] for key in ("artifact_id", "artifact_type", "file_path", "producer",
            "content_sha256", "size_bytes", "exists")}
        receipt["recorded_at"] = _utc_now().isoformat()
        history = _json_load(run.artifact_receipts_json, [])
        if history and any(item.get("artifact_id") != record["artifact_id"]
                           or item.get("content_sha256") != record["content_sha256"] for item in history):
            raise DurableJobLeaseError("original native read admits exactly one artifact")
        if not history:
            run.artifact_receipts_json = _canonical([receipt])
            run.revision += 1
            run.updated_at = _utc_now()
        return record

    async def record_native_read_effect_in_session(self, db, run, *, operation, call_ref,
            effect_type, effect_id, status, receipt_kind="effect", target_path=None,
            content_sha256=None, readback_id=None, details=None):
        from src.runtime_plugins.read_journal import validate_operation, _artifact_writer
        _artifact_writer(db, run)
        validate_operation(run, operation, call_ref=call_ref)
        candidate = operation.candidate()
        from src.runtime_plugins.read_artifacts import native_read_artifact_path
        from src.runtime_plugins.read_journal import _operation_ref
        if (effect_type != "native_read_artifact" or status not in {"intent", "succeeded"}
            or receipt_kind not in {"effect", "readback"} or type(effect_id) is not str
            or effect_id != _operation_ref(run, candidate["method"])
            or content_sha256 != candidate["content_digest"]
            or (status == "intent" and (candidate["method"] != "artifacts.stage" or receipt_kind != "effect"))
            or (status == "succeeded" and (receipt_kind != "readback" or readback_id != effect_id + ":readback"))):
            raise DurableJobLeaseError("original native artifact effect binding changed")
        reference = target_path or ""
        if (reference != native_read_artifact_path(run, candidate["content_digest"])
            or not reference.startswith("artifacts/work-board/runtime-service-read/") or ".." in reference.split("/")):
            raise DurableJobLeaseError("original native artifact effect path changed")
        history = _effect_ledger_or_raise(run.effect_receipts_json)
        previous = next((item for item in history if item.get("effect_id") == effect_id), None)
        if previous is not None:
            if (previous.get("target_path") != target_path or previous.get("content_sha256") != content_sha256
                or previous.get("effect_type") != effect_type or previous.get("status") != "intent"
                or status != "succeeded" or receipt_kind != "readback"):
                raise DurableJobLeaseError("original native artifact effect replay denied")
        receipt = {"effect_id": effect_id, "receipt_kind": receipt_kind, "effect_type": effect_type,
            "target_path": target_path, "target_digest": content_sha256, "approval_id": None,
            "adapter_idempotency_key": effect_id, "status": status, "content_sha256": content_sha256,
            "details": _safe_structure(details or {}), "recorded_at": _utc_now().isoformat(),
            "fencing_token": run.fencing_token}
        if receipt_kind == "readback":
            receipt.update(readback_id=readback_id, verified_at=_utc_now().isoformat())
        run.effect_receipts_json = _canonical(_job_effect_ledger(run,
            [item for item in history if item.get("effect_id") != effect_id] + [receipt]))
        run.revision += 1
        run.updated_at = _utc_now()
        return receipt

    @_deny_original_memory_generic_entry
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
        native_report_witness=None,
    ) -> dict[str, Any]:
        """Persist an operator-safe artifact receipt.

        An ownerless, content-free receipt is allowed only in ``accepted``
        before execution is claimed. A leased/running job must supply the
        current lease owner and fencing token so a stale runner cannot append
        an artifact after restart recovery.
        """
        async with self._writer_session() as db:
            run = await self._fetch(db, job_id)
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")
            if run.job_kind == "work.local-evidence-report.v1" and run.composition_binding_json is not None:
                if native_report_witness is None:
                    raise DurableJobLeaseError("original report artifact source witness required")
                if artifact_type != "evidence_local_report" or not isinstance(content, bytes):
                    raise DurableJobLeaseError("fixed original report artifact required")
                await db.rollback()
                from src.runtime_plugins.ownership import begin_native_writer
                await begin_native_writer(db, owner="durable_jobs")
                run = await self._fetch(db, job_id)
                from src.runtime_plugins.task_capability import recheck_report_current
                await recheck_report_current(db, run, phase="artifact", witness=native_report_witness,
                    file_path=file_path, content_digest=hashlib.sha256(content.encode() if isinstance(content, str) else content or b"").hexdigest())
            from src.workflows.general_task_guard import requires_native_writer, verify_native_writer
            if requires_native_writer(run):
                await db.rollback()
                from src.work_board.repository import _begin_sqlite_immediate
                await _begin_sqlite_immediate(db)
                run = await self._fetch(db, job_id)
                await verify_native_writer(self, db, run)
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    artifact_receipts_json=_canonical(_github_recovery_history(run, existing, kind="artifact")),
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

    @_deny_original_memory_generic_entry
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
        async with self._writer_session() as db:
            run = await self._fetch(db, job_id)
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")
            from src.workflows.general_task_guard import requires_native_writer, verify_native_writer
            if requires_native_writer(run):
                raise DurableJobLeaseError("native general task recovery cannot adopt an unbound artifact")
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    artifact_receipts_json=_canonical(_github_recovery_history(run, existing, kind="artifact")),
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

    async def inspect_github_capacity_close(self, job_id, *, request, principal, root, native_kind):
        """Inspect one pending close in a single canonical read snapshot.

        Monotonic revision/fence supersession proves that this exact old CAS
        cannot apply. A matching snapshot alone proves nothing about an
        in-flight request, so it remains inconclusive. No GET/provider/file
        work, mutation or new idempotency ledger occurs in this transaction.
        """
        from src.db.models import GitHubFollowthroughConnection, OperatorSession
        from src.extensions.github_capacity_closure import public_closure
        from src.extensions.github_consent import live_operator, utc
        from src.extensions.github_recovery import KINDS, job_binding
        from src.work_board.repository import _begin_read_snapshot
        await live_operator(principal, root)
        if native_kind not in KINDS:
            raise DurableJobTransitionError("GitHub pending close kind invalid")
        body = request.model_dump(mode="json")
        request_digest = _digest(body)
        async with self._session() as db:
            await _begin_read_snapshot(db)
            run = await self._fetch(db, job_id, allow_closed=True)
            if run.job_kind != native_kind or run.owner_kind != "user" or run.owner_principal_id != principal or run.operator_session_id != root:
                raise DurableJobLeaseError("GitHub pending close original owner/root mismatch")
            session = await db.get(OperatorSession, root)
            now = _utc_now()
            if session is None or session.principal_id != principal or session.revoked_at is not None or session.replaced_by_id is not None or session.is_bearer_tombstone or utc(session.idle_expires_at) <= now or utc(session.absolute_expires_at) <= now:
                raise DurableJobLeaseError("GitHub pending close original root dead")
            authority = _json_load(run.declared_authority_json, {})
            original = authority.get("github_consent") if isinstance(authority, dict) else None
            if _digest(authority) != run.authority_digest or not isinstance(original, dict) or original.get("owner_principal_id") != principal or original.get("consent_root_id") != root:
                raise DurableJobLeaseError("GitHub pending close immutable binding invalid")
            connection = await db.get(GitHubFollowthroughConnection, original.get("connection_id"))
            state = "inconclusive"
            closure = None
            if run.github_capacity_closure_json:
                history = _json_load(run.github_capacity_closure_json, {})
                valid = isinstance(history, dict) and history.get("history_digest") == _digest({key: value for key, value in history.items() if key != "history_digest"})
                binding = history.get("binding") if isinstance(history, dict) else None
                if valid and isinstance(binding, dict) and binding.get("original_write_binding") == original and binding.get("job") == job_binding(run) and history.get("native_kind") == native_kind and history.get("observation_only") is True:
                    if history.get("request") == body and history.get("request_digest") == request_digest:
                        state = "applied"
                        closure = public_closure(run.github_capacity_closure_json)
                    else:
                        # The permanent old-job write fence makes every
                        # different close body permanently non-applicable.
                        state = "permanently_stale_not_applied"
            elif connection is not None and connection.owner_principal_id == principal and connection.repository == original.get("repository"):
                if run.revision > request.expected_job_revision or connection.revision > request.expected_connection_revision or connection.active_fence > request.expected_connection_fence:
                    state = "permanently_stale_not_applied"
            projection = _serialize(run)
            projection["pending_capacity_close"] = {
                "state": state, "job_id": job_id, "job_revision": run.revision,
                "request": request.model_dump(mode="json", exclude_none=True),
                "request_digest": request_digest, "closure": closure,
            }
            return projection

    async def get_github_capacity_closure(self, job_id, *, request, principal, root):
        """Exact lost-response lookup; no provider or reservation contact."""
        from src.extensions.github_consent import live_operator
        from src.extensions.github_recovery import check_binding
        await live_operator(principal, root)
        async with self._session() as db:
            run = await self._fetch(db, job_id, allow_closed=True)
            if run.owner_kind != "user" or run.owner_principal_id != principal or run.operator_session_id != root:
                raise DurableJobLeaseError("GitHub closure original owner/root mismatch")
            if not run.github_capacity_closure_json:
                return None
            existing = _json_load(run.github_capacity_closure_json, {})
            body = request.model_dump(mode="json")
            if existing.get("history_digest") != _digest({key: value for key, value in existing.items() if key != "history_digest"}) or existing.get("request") != body or existing.get("request_digest") != _digest(body):
                raise DurableJobIdempotencyConflict("GitHub capacity already differently closed")
            await check_binding(db, run, existing["binding"], reserved=None, board=False, ignore_read_revision=True)
            db.expunge(run)
            return _serialize(run, receipt={"kind": "github_capacity_closure", "status": "already_recorded", "closure_id": existing["closure_id"], "observation_only": True})

    async def record_github_capacity_closure(self, job_id, *, request, read_authority, proof):
        """Receipt-only closure and exact reservation CAS in one short session.

        The caller holds the private producer guard through commit. All GET,
        authentication, terminal inspection and private bytes work precedes
        BEGIN IMMEDIATE. The old effect/Goal/finance/outcome fields are intact.
        """
        from src.extensions.github_capacity_closure import (_CompleteClosureProof,
            _CLOSURE_SEAL, PublicationCloseRequest, LegacyCloseRequest,
            original_effects, effect_identity)
        from src.extensions.github_consent import GitHubReadbackAuthority, GitHubVerifiedReadback
        from src.extensions.github_recovery import check_binding
        from src.db.models import GitHubFollowthroughConnection
        from src.workflows.repo_publication import write_file, read_file
        import time
        import uuid
        if type(proof) is not _CompleteClosureProof or proof._seal is not _CLOSURE_SEAL or type(read_authority) is not GitHubReadbackAuthority or read_authority.job_id != job_id:
            raise DurableJobLeaseError("protected complete GitHub closure proof required")
        expected_type = PublicationCloseRequest if proof.original_job.get("job_kind") == "engineering.repo-publication.v1" else LegacyCloseRequest
        if type(request) is not expected_type or proof.original_job.get("job_id") != job_id:
            raise DurableJobLeaseError("fixed GitHub closure request kind required")
        await read_authority.validate()
        originals = original_effects(proof.original_job)
        remote = [item for item in originals if item.get("effect_type") != "repo_publication_local_producer"]
        verified = {}
        for actual, identity in proof.positive_gets:
            if type(actual) is not GitHubVerifiedReadback or identity.get("effect_id") in verified or not actual.validates(read_authority,
                {"readback_path": actual.readback_path, "payload_sha256": actual.payload_sha256}, identity, max_age_seconds=120) or actual.canonical_binding != proof.binding:
                raise DurableJobLeaseError("GitHub closure actual semantic GET set invalid")
            verified[identity["effect_id"]] = identity
        if set(verified) != {item["effect_id"] for item in remote} or any(verified.get(item["effect_id"]) != effect_identity(proof.original_job, item) for item in remote):
            raise DurableJobLeaseError("GitHub closure complete intent proof missing")
        if expected_type is PublicationCloseRequest and not isinstance(proof.producer, dict):
            raise DurableJobLeaseError("GitHub closure actual producer terminal required")
        if time.monotonic() >= proof.deadline:
            raise DurableJobLeaseError("GitHub closure whole-operation deadline expired")
        request_body = request.model_dump(mode="json")
        request_digest = _digest(request_body)
        closure_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"seraph:github-capacity-close:{job_id}:{read_authority.root}:{request_digest}"))
        closed_at = _utc_now().isoformat()
        get_set = [{"identity": identity, "path": actual.readback_path,
            "raw_payload_sha256": actual.payload_sha256, "semantic_payload_sha256": actual.semantic_payload_sha256}
            for actual, identity in proof.positive_gets]
        value = {"schema": "seraph.github-capacity-closure.v1", "closure_id": closure_id,
            "job_id": job_id, "native_kind": proof.original_job["job_kind"],
            "closed_at": closed_at, "observation_only": True, "learning": "no_learning",
            "job_status": proof.original_job["status"], "original_job_revision": request.expected_job_revision,
            "request_digest": request_digest, "idempotency_key": request.idempotency_key,
            "effect_inventory_sha256": proof.effect_inventory_sha256,
            "positive_get_set_sha256": _digest(get_set), "positive_get_count": len(get_set),
            "producer": proof.producer, "window": proof.window}
        content = _canonical(value).encode()
        if len(content) > 256 * 1024:
            raise ValueError("GitHub closure private artifact bounds invalid")
        sha = hashlib.sha256(content).hexdigest()
        path = f"artifacts/github-capacity-closures/{job_id}/{sha}.json"
        write_file(path, content)
        if read_file(path, maximum=256 * 1024) != content:
            raise ValueError("GitHub closure private bytes readback failed")
        record = build_artifact_record(file_path=path, artifact_type="github_capacity_closure",
            producer=proof.original_job["job_kind"], run_id=job_id,
            session_id=proof.original_job["session_id"], content=content)
        closure = {**value, "artifact_id": record["artifact_id"], "artifact_sha256": sha,
            "positive_gets": get_set,
            "artifact_path": path, "binding": proof.binding, "request": request_body}
        closure["history_digest"] = _digest(closure)
        if len(_canonical(closure).encode()) > 4 * 1024 * 1024:
            raise DurableJobLeaseError("GitHub closure canonical proof set bounds invalid")
        async with self._writer_session() as db:
            if getattr(getattr(db.get_bind(), "dialect", None), "name", "") == "sqlite":
                await _begin_legacy_aware_writer(db)
            run = await self._fetch(db, job_id, allow_closed=True)
            if run.github_capacity_closure_json:
                existing = _json_load(run.github_capacity_closure_json, {})
                if existing.get("history_digest") != _digest({key: value for key, value in existing.items() if key != "history_digest"}) or existing.get("request_digest") != request_digest or existing.get("request") != request_body:
                    raise DurableJobIdempotencyConflict("GitHub capacity already differently closed")
                await check_binding(db, run, existing["binding"], reserved=None, board=False, ignore_read_revision=True)
                return _serialize(run, receipt={"kind": "github_capacity_closure", "status": "already_recorded", "closure_id": existing["closure_id"]})
            connection = await check_binding(db, run, proof.binding)
            if run.status not in {"unknown_external_effect", "blocked", "failed"} or run.lease_owner or run.lease_expires_at:
                raise DurableJobLeaseError("GitHub capacity close requires an unleased recovery job")
            if type(request.expected_job_revision) is not int or run.revision != request.expected_job_revision or proof.binding["minted_job_revision"] != run.revision or run.revision != proof.original_job["revision"] or request.expected_connection_revision != connection.revision or request.expected_connection_fence != connection.active_fence:
                raise DurableJobLeaseError("GitHub closure exact revisions/fence changed")
            if _digest(_effect_ledger_or_raise(run.effect_receipts_json)) != proof.effect_inventory_sha256 or time.monotonic() >= proof.deadline:
                raise DurableJobLeaseError("GitHub closure inventory or deadline changed")
            if proof.producer is not None:
                admission = next((item.get("payload") for item in _json_load(run.checkpoint_receipts_json, []) if item.get("checkpoint_id") == "publication_supervisor_admission"), None)
                if admission != proof.producer["canonical_admission"]:
                    raise DurableJobLeaseError("GitHub closure canonical supervisor admission changed")
            artifact = {key: record[key] for key in ("artifact_id", "artifact_type", "file_path", "producer", "content_sha256", "size_bytes", "exists")}
            artifact["recorded_at"] = closed_at
            artifacts = _json_load(run.artifact_receipts_json, []) + [artifact]
            checkpoint = {"checkpoint_id": "github_capacity_closed:" + closure_id, "safe": True, "recorded_at": closed_at,
                "payload": {"closure_id": closure_id, "artifact_id": record["artifact_id"], "artifact_sha256": sha,
                    "request_digest": request_digest, "permanent_execution_fence": True, "observation_only": True}}
            checkpoints = _json_load(run.checkpoint_receipts_json, []) + [checkpoint]
            updated = await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False).where(
                WorkflowRunState.run_identity == job_id, WorkflowRunState.revision == run.revision,
                WorkflowRunState.status == run.status, WorkflowRunState.lease_owner.is_(None),
                WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.github_capacity_closure_json.is_(None)).values(
                    github_capacity_closure_json=_canonical(closure), artifact_receipts_json=_canonical(_github_recovery_history(run, artifacts, kind="artifact")),
                    checkpoint_receipts_json=_canonical(_github_recovery_history(run, checkpoints, kind="checkpoint")), revision=WorkflowRunState.revision+1))
            released = await db.execute(update(GitHubFollowthroughConnection).execution_options(synchronize_session=False).where(
                GitHubFollowthroughConnection.id == connection.id, GitHubFollowthroughConnection.owner_principal_id == read_authority.principal,
                GitHubFollowthroughConnection.repository == connection.repository, GitHubFollowthroughConnection.vault_key == connection.vault_key,
                GitHubFollowthroughConnection.revision == request.expected_connection_revision,
                GitHubFollowthroughConnection.active_job_id == job_id,
                GitHubFollowthroughConnection.active_fence == request.expected_connection_fence).values(active_job_id=None))
            if not _rowcount_is_one(updated) or not _rowcount_is_one(released):
                raise DurableJobLeaseError("GitHub closure atomic reservation CAS changed")
            refreshed = await self._fetch(db, job_id, allow_closed=True)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "github_capacity_closure", "status": "recorded", "closure_id": closure_id, "observation_only": True})

    async def record_github_recovery_observation(
        self, job_id: str, *, read_authority, verified_readback,
        expected_revision: int, expected_attempt_count: int,
        expected_authority_digest: str, effect_id: str, effect_type: str,
        target_path: str, target_digest: str, adapter_idempotency_key: str | None,
        readback_id: str, verified_at: str, artifact_content: bytes,
        artifact_sha256: str,
    ) -> dict[str, Any]:
        """Append verified GitHub evidence without adopting changed Goal authority.

        This seam cannot contact a provider, resume, release a reservation, or
        finalize a job. Original effects remain intact; the appended readback
        links to one exact already-contacted effect. General lifecycle and
        artifact APIs keep their existing Goal fences.
        """
        from src.extensions.github_consent import GitHubReadbackAuthority, GitHubClosedReadbackAuthority, GitHubVerifiedReadback
        closed_read = type(read_authority) is GitHubClosedReadbackAuthority
        if type(read_authority) not in {GitHubReadbackAuthority, GitHubClosedReadbackAuthority} or read_authority.job_id != job_id:
            raise DurableJobLeaseError("canonical GitHub readback authority required")
        if type(artifact_content) is not bytes or not 1 <= len(artifact_content) <= 256 * 1024:
            raise ValueError("GitHub observation artifact bounds invalid")
        if re.fullmatch(r"[0-9a-f]{64}", artifact_sha256 or "") is None or hashlib.sha256(artifact_content).hexdigest() != artifact_sha256:
            raise ValueError("GitHub observation artifact digest invalid")
        try:
            observation = json.loads(artifact_content)
            verified = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("GitHub observation schema invalid") from exc
        expected = {"schema": "seraph.github-effect-observation.v1", "job_id": job_id,
            "attempt_count": expected_attempt_count, "authority_digest": expected_authority_digest,
            "effect_id": effect_id, "effect_type": effect_type, "target_path": target_path,
            "target_digest": target_digest, "adapter_idempotency_key": adapter_idempotency_key,
            "readback_id": readback_id, "verified_at": verified_at}
        if not isinstance(observation, dict) or set(observation) != set(expected) | {"remote_readback"} or any(observation.get(key) != value for key, value in expected.items()) or not isinstance(observation["remote_readback"], dict):
            raise ValueError("GitHub observation schema or identity invalid")
        identity = {key: expected[key] for key in ("job_id", "attempt_count", "authority_digest", "effect_id", "effect_type", "target_path", "target_digest", "adapter_idempotency_key")}
        if type(verified_readback) is not GitHubVerifiedReadback or not verified_readback.validates(read_authority, observation["remote_readback"], identity):
            raise ValueError("GitHub observation actual adapter GET proof invalid")
        if not readback_id or len(readback_id) > 200 or verified.tzinfo is None or verified > _utc_now() or (_utc_now()-verified).total_seconds() > 300:
            raise ValueError("GitHub observation verification identity invalid")
        await read_authority.validate()
        # All private filesystem work and authentication precedes the write
        # transaction. A staged immutable orphan is harmless on CAS failure.
        from src.workflows.repo_publication import write_file, read_file
        from src.extensions.github_recovery import check_binding, check_closed_binding
        binding = verified_readback.canonical_binding
        if binding.get("minted_job_revision") != expected_revision:
            raise DurableJobLeaseError("GitHub observation protected mint revision changed")
        file_path = f"artifacts/github-observations/{job_id}/{artifact_sha256}.json"
        write_file(file_path, artifact_content)
        if read_file(file_path, maximum=256 * 1024) != artifact_content:
            raise ValueError("GitHub observation private artifact readback failed")
        record = build_artifact_record(file_path=file_path, artifact_type="github_recovery_observation",
            producer=read_authority.capability, run_id=job_id,
            session_id=binding["job"]["session_id"], content=artifact_content)
        async with self._writer_session() as db:
            if getattr(getattr(db.get_bind(), "dialect", None), "name", "") == "sqlite":
                await _begin_legacy_aware_writer(db)
            run = await self._fetch(db, job_id, allow_closed=closed_read)
            if closed_read:
                await check_closed_binding(db, run, read_authority, binding=binding)
            else:
                await check_binding(db, run, binding)
            if not verified_readback.validates(read_authority, observation["remote_readback"], identity):
                raise DurableJobLeaseError("GitHub observation protected proof expired or changed")
            if run.github_capacity_closure_json and not closed_read:
                raise DurableJobLeaseError("GitHub capacity is permanently closed")
            allowed_types = {
                "github_followthrough_v1": {"github_publication"},
                "engineering.repo-publication.v1": {"repo_publication_branch", "repo_publication_pr", "repo_publication_commit", "repo_publication_tree"},
            }
            known_blob = run.job_kind == "engineering.repo-publication.v1" and re.fullmatch(r"repo_publication_blob_[0-9a-f]{16}", effect_type or "") is not None
            if run.job_kind not in allowed_types or read_authority.capability != run.job_kind or (effect_type not in allowed_types[run.job_kind] and not known_blob):
                raise DurableJobTransitionError("GitHub observation capability or effect invalid")
            if run.owner_kind != "user" or run.owner_principal_id != read_authority.principal or run.operator_session_id != read_authority.root:
                raise DurableJobLeaseError("GitHub observation original owner/root mismatch")
            if run.status not in {"unknown_external_effect", "cost_liability", "blocked", "failed"} or run.lease_owner or run.lease_expires_at:
                raise DurableJobLeaseError("GitHub observation requires an unleased recovery job")
            if type(expected_revision) is not int or _revision(run) != expected_revision or type(expected_attempt_count) is not int or run.attempt_count != expected_attempt_count or run.authority_digest != expected_authority_digest:
                raise DurableJobLeaseError("GitHub observation job revision/attempt/authority changed")
            authority = _json_load(run.declared_authority_json, {})
            if authority.get("github_consent") != read_authority.original_binding:
                raise DurableJobLeaseError("GitHub observation original connection binding changed")
            effects = _effect_ledger_or_raise(run.effect_receipts_json)
            prior = next((item for item in effects if item.get("effect_id") == effect_id), None)
            if not prior or prior.get("effect_type") != effect_type or prior.get("target_path") != target_path or prior.get("target_digest") != target_digest or prior.get("adapter_idempotency_key") != adapter_idempotency_key or (prior.get("status") not in UNRESOLVED_EFFECT_STATUSES and not closed_read):
                raise DurableJobIdempotencyConflict("GitHub observation prior contacted effect missing or changed")
            if closed_read:
                history = _json_load(run.github_capacity_closure_json, {})
                if not any(item.get("identity") == identity and item.get("path") == verified_readback.readback_path for item in history.get("positive_gets", [])):
                    raise DurableJobLeaseError("GitHub closed observation original positive effect missing")
            prefix = "/repos/" + str(read_authority.original_binding.get("repository") or "") + "/"
            if not target_path.startswith(prefix) or re.fullmatch(r"[0-9a-f]{64}", target_digest or "") is None:
                raise ValueError("GitHub observation target invalid")
            observation_id = effect_id + ":observation:" + artifact_sha256[:16]
            repeated = next((item for item in effects if item.get("effect_id") == observation_id), None)
            if repeated:
                if repeated.get("content_sha256") != artifact_sha256:
                    raise DurableJobIdempotencyConflict("GitHub observation id already bound")
                return _serialize(run, receipt={"kind": "github_recovery_observation", "observation_only": True, "status": "already_recorded"})
            recorded_at = _utc_now().isoformat()
            artifact = {key: record[key] for key in ("artifact_id", "artifact_type", "file_path", "producer", "content_sha256", "size_bytes", "exists")}
            artifact["recorded_at"] = recorded_at
            effects.append({"effect_id": observation_id, "receipt_kind": "readback", "effect_type": effect_type,
                "target_path": target_path, "target_digest": target_digest, "adapter_idempotency_key": adapter_idempotency_key,
                "status": "succeeded", "content_sha256": artifact_sha256, "readback_id": readback_id,
                "verified_at": verified_at, "recorded_at": recorded_at,
                "details": {"observation_only": True, "original_effect_id": effect_id, "artifact_id": record["artifact_id"]}})
            checkpoints = _json_load(run.checkpoint_receipts_json, [])
            checkpoints.append({"checkpoint_id": observation_id, "safe": True, "recorded_at": recorded_at,
                "payload": {"observation_only": True, "original_effect_id": effect_id,
                    "readback_id": readback_id, "content_sha256": artifact_sha256, "artifact_id": record["artifact_id"]}})
            artifacts = _json_load(run.artifact_receipts_json, [])
            artifacts.append(artifact)
            # The protected envelope is never supplied through an API. Its
            # exact effect readback is derived from the actual adapter seal.
            protected = {"schema": "seraph.github-read-revision-receipt.v1",
                "observation_id": observation_id,
                "observation_artifact_id": record["artifact_id"],
                "observation_artifact_sha256": artifact_sha256,
                "receipt_id": "github-read:" + _digest({"job": job_id, "readback": readback_id, "artifact": artifact_sha256}),
                "public_capability": "work.github-followthrough.v1" if run.job_kind == "github_followthrough_v1" else run.job_kind,
                "binding": binding, "registered_job_revision": expected_revision + 1,
                "readback_path": verified_readback.readback_path,
                "raw_payload_sha256": verified_readback.payload_sha256,
                "semantic_payload_sha256": verified_readback.semantic_payload_sha256,
                "effect_identity": identity,
                "effect_readback": {"effect_id": effect_id, "effect_type": effect_type,
                    "target_path": target_path, "target_digest": target_digest,
                    "adapter_idempotency_key": adapter_idempotency_key,
                    "readback_id": readback_id, "verified_at": verified_at,
                    "content_sha256": verified_readback.semantic_payload_sha256}}
            protected["receipt_digest"] = _digest(protected)
            observation_history = _json_load(run.github_read_observation_history_json, [])
            if not isinstance(observation_history, list) or len(observation_history) >= 4096:
                raise DurableJobLeaseError("GitHub protected observation history bounds invalid")
            observation_history = observation_history + [protected]
            history_json = _canonical(observation_history)
            if len(history_json.encode()) > 4 * 1024 * 1024:
                raise DurableJobLeaseError("GitHub protected observation history bytes invalid")
            updated = await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False).where(
                WorkflowRunState.run_identity == job_id, WorkflowRunState.revision == expected_revision,
                WorkflowRunState.status == run.status, WorkflowRunState.lease_owner.is_(None),
                WorkflowRunState.lease_expires_at.is_(None)).values(
                    effect_receipts_json=_canonical(_job_effect_ledger(run, effects)),
                    checkpoint_receipts_json=_canonical(_github_recovery_history(run, checkpoints, kind="checkpoint")),
                    artifact_receipts_json=_canonical(_github_recovery_history(run, artifacts, kind="artifact")),
                    github_read_observation_history_json=history_json,
                    github_read_revision_json=run.github_read_revision_json if closed_read else _canonical(protected), revision=WorkflowRunState.revision+1))
            if not _rowcount_is_one(updated):
                raise DurableJobLeaseError("GitHub observation CAS changed")
            try:
                await _assert_canonical_goal_fence(db, goal_id=run.goal_id,
                    goal_revision=run.goal_revision, owner_kind=run.owner_kind,
                    owner_principal_id=run.owner_principal_id, session_id=run.session_id,
                    authority=run.declared_authority_json)
                goal_matches = True
            except DurableJobTransitionError:
                goal_matches = False
            if closed_read:
                goal_matches = False
            refreshed = await self._fetch(db, job_id, allow_closed=closed_read)
            db.expunge(refreshed)
            return _serialize(refreshed, receipt={"kind": "github_recovery_observation", "observation_only": True,
                "current_goal_matches": goal_matches, "blocked_current_goal": not goal_matches,
                "capacity_closed": closed_read,
                "artifact_id": record["artifact_id"], "content_sha256": artifact_sha256})

    @_deny_original_memory_generic_entry
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
        readback_authority_check: Callable[[Any, Any], Awaitable[None]] | None = None,
        native_report_witness=None,
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
        async with self._writer_session() as db:
            if readback_authority_check is not None:
                if receipt_kind != "readback":
                    raise ValueError("authority callback requires a readback receipt")
                from src.work_board.repository import _begin_sqlite_immediate
                await _begin_sqlite_immediate(db)
            run = await self._fetch(db, job_id)
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")
            if run.job_kind == "work.local-evidence-report.v1" and run.composition_binding_json is not None:
                if native_report_witness is None or effect_type != "evidence_cpu_output":
                    raise DurableJobLeaseError("fixed original report effect source witness required")
                await db.rollback()
                from src.runtime_plugins.ownership import begin_native_writer
                await begin_native_writer(db, owner="durable_jobs")
                run = await self._fetch(db, job_id)
                from src.runtime_plugins.task_capability import recheck_report_current
                await recheck_report_current(db, run, phase="readback" if receipt_kind == "readback" else "effect",
                    witness=native_report_witness, file_path=target_path, content_digest=content_sha256 or target_digest)
            from src.workflows.general_task_guard import requires_native_writer, verify_native_writer
            if requires_native_writer(run):
                if readback_authority_check is None:
                    await db.rollback()
                    from src.work_board.repository import _begin_sqlite_immediate
                    await _begin_sqlite_immediate(db)
                    run = await self._fetch(db, job_id)
                await verify_native_writer(self, db, run)
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
            if run.job_kind == "forgejo_issue_title_v1" and readback_authority_check is None:
                raise DurableJobTransitionError("Forgejo effects require its fixed native authority callback")
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
            if readback_authority_check is not None:
                await readback_authority_check(db, run)
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
                precontact_absence = (
                    run.job_kind == "agent.task.v1" and effect_type == "general_tool_call"
                    and isinstance(safe_details, dict)
                    and safe_details.get("verified") is True
                    and safe_details.get("never_contacted") is True
                    and safe_details.get("approval_precontact") is True
                    and any(isinstance(item.get("payload"), dict)
                        and item["payload"].get("phase") == "approval_precontact"
                        and item["payload"].get("effect_id") == effect_id
                        and item["payload"].get("fence") == fencing_token
                        and _digest(item["payload"]) == content_sha256
                        for item in _json_load(run.checkpoint_receipts_json, []))
                )
                if not _verified_readback_exists([receipt]) and not precontact_absence:
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
            result_update = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    effect_receipts_json=_canonical(_job_effect_ledger(run, existing)),
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

    @_deny_original_memory_generic_entry
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
        readback_authority_check: Callable[[Any, Any], Awaitable[None]] | None = None,
        native_report_witness=None,
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
            readback_authority_check=readback_authority_check,
            native_report_witness=native_report_witness,
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
        await _deny_original_memory_generic_body(self, job_id)
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

    @_deny_original_memory_generic_entry
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
        async with self._writer_session() as db:
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
                if run.job_kind == "readonly_research_child" and previous.get("status") == "intent":
                    from src.work_board.research_control import precontact_intent_reusable
                    if (all(previous_details.get(key) == value for key, value in safe_details.items())
                        and await precontact_intent_reusable(self, db, run, effects)):
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
            effects.append(receipt)
            updated = await db.execute(
                update(WorkflowRunState)
                .execution_options(synchronize_session=False)
                .where(*conditions)
                .values(
                    effect_receipts_json=_canonical(_job_effect_ledger(run, effects)),
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

    @_deny_original_memory_generic_entry
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
        async with self._writer_session() as db:
            run = await self._fetch(db, job_id)
            if run.job_kind == "runtime_service_memory_v1":
                raise DurableJobLeaseError("original_memory_generic_mutation_unavailable")
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
            if _native_turn_pending(run):
                raise DurableJobTransitionError("original native turn physical completion is unproven; retry denied")
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
                    effect_receipts_json=_canonical(_job_effect_ledger(run, existing_effects)),
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
        from src.extensions.github_recovery import KINDS, check_persisted_readback
        preliminary = await self.get_job(job_id)
        github_recovery = preliminary is not None and preliminary.get("job_kind") in KINDS
        if github_recovery:
            from src.extensions.github_consent import live_operator
            await live_operator(owner_principal_id, preliminary["operator_session_id"])
        async with self._writer_session() as db:
            if github_recovery and getattr(getattr(db.get_bind(), "dialect", None), "name", "") == "sqlite":
                await _begin_legacy_aware_writer(db)
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
            try:
                protected = await check_persisted_readback(db, run) if github_recovery else None
            except (ValueError, KeyError, TypeError) as exc:
                raise DurableJobLeaseError(str(exc)) from exc
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
            await _verify_native_child_sql_scope(db, run)
            _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
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
            if protected is not None:
                protected.pop("receipt_digest")
                protected["finalized_job_revision"] = current_revision + 1
                protected["receipt_digest"] = _digest(protected)
                values["github_read_revision_json"] = _canonical(protected)
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
        async with self._writer_session() as db:
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
                    effect_receipts_json=_canonical(_job_effect_ledger(run, resolved_effects)),
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

    @_original_memory_maintenance_entry
    async def recover_stale_jobs(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        observed_at = now or _utc_now()
        recovered: list[dict[str, Any]] = await self.recover_inference_accounting(now=observed_at)
        async with self._writer_session() as db:
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
                    updated = await _execute_original_memory_negative(self, db, run,
                        recovery_conditions, {"status": "blocked", "failure_reason": "stale_goal_revision",
                            "lease_owner": None, "lease_expires_at": None, "updated_at": observed_at,
                            "heartbeat_at": observed_at, "revision": WorkflowRunState.revision + 1,
                            "fencing_token": WorkflowRunState.fencing_token + 1},
                        receipt={"kind": "restart_recovery", "status": "blocked", "reason": "stale_goal_revision",
                            "recovery_state": "stale_goal_revision", "previous_owner": old_owner,
                            "fencing_token": int(run.fencing_token or 0) + 1, "revision": expected_revision + 1,
                            "operator_action": "discard_stale_goal_revision_before_new_admission",
                            "stale_goal_revision": True, "operator_visible": True})
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
                    if _native_turn_pending(run):
                        recovered_status, recovery_reason = _restart_recovery_state(run)
                        recovery_effects = None
                    elif persisted_deadline is not None and persisted_deadline <= observed_at:
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
                            if run.job_kind != "runtime_service_memory_v1" and effects is not None and not _job_has_unsafe_effects(effects)
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
                if _native_turn_pending(run):
                    recovered_status, recovery_reason = await self.native_turn_family_recovery_state_in_session(db, run)
                    recovery_effects = None
                if (run.job_kind == "work.local-evidence-report.v1" and run.composition_binding_json
                    and run.attempt_count == 1):
                    if recovered_status != "cost_liability":
                        recovered_status, recovery_reason = "unknown_external_effect", "native_report_original_closure_unproven"
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
                        _job_effect_ledger(run, recovery_effects)
                    )
                recovery_conditions = [
                    WorkflowRunState.run_identity == run.run_identity,
                    WorkflowRunState.status == "running",
                    WorkflowRunState.revision == expected_revision,
                    WorkflowRunState.fencing_token == expected_token,
                ]
                _append_goal_fence_condition(recovery_conditions, run)
                updated = await _execute_original_memory_negative(self, db, run,
                    recovery_conditions, recovery_values,
                    receipt={"kind": "restart_recovery", "status": recovered_status,
                        "reason": recovery_reason, "recovery_state": recovered_status,
                        "previous_owner": old_owner, "fencing_token": int(run.fencing_token or 0) + 1,
                        "revision": expected_revision + 1,
                        "operator_action": "deadline_expired_no_retry"
                            if recovered_status == "failed" and recovery_reason == "deadline_expired"
                            else ("resume_already_settled_remote_operation" if recovered_status == "succeeded"
                                else ("reconcile_external_effect_and_cost_then_retry_or_cancel"
                                    if recovered_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES
                                    else "reconcile_effects_then_retry_or_cancel")),
                        "operator_visible": True})
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

    @_original_memory_maintenance_entry
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
        async with self._writer_session() as db:
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
            if _native_turn_pending(run):
                recovery_status, recovery_reason = await self.native_turn_family_recovery_state_in_session(db, run)
            if (run.job_kind == "work.local-evidence-report.v1" and run.composition_binding_json
                and run.attempt_count == 1):
                if recovery_status != "cost_liability":
                    recovery_status, recovery_reason = "unknown_external_effect", "native_report_original_closure_unproven"
            expected_revision = _revision(run)
            expected_fence = int(run.fencing_token or 0)
            conditions = [
                WorkflowRunState.run_identity == job_id,
                WorkflowRunState.status == "running",
                WorkflowRunState.revision == expected_revision,
                WorkflowRunState.fencing_token == expected_fence,
            ]
            _append_goal_fence_condition(conditions, run)
            updated = await _execute_original_memory_negative(self, db, run, conditions,
                {"status": recovery_status, "failure_reason": recovery_reason,
                    "lease_owner": None, "lease_expires_at": None,
                    "fencing_token": WorkflowRunState.fencing_token + 1,
                    "revision": WorkflowRunState.revision + 1, "updated_at": observed_at,
                    "heartbeat_at": observed_at, "finished_at": observed_at if recovery_status == "failed" else None},
                receipt={"kind": "targeted_recovery", "status": recovery_status, "reason": recovery_reason,
                    "previous_owner": None, "fencing_token": expected_fence + 1,
                    "revision": expected_revision + 1, "operator_visible": True})
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

    async def _fetch(self, db: Any, job_id: str, *, allow_closed=False) -> WorkflowRunState:
        await _original_memory_maintenance_snapshot(self, db, writer=True)
        from src.workflows.durable_state import (_CURRENT_LEGACY_RECOVERY,
            _begin_legacy_aware_writer, _legacy_writer_budget)
        source = _CURRENT_LEGACY_RECOVERY.get()
        if source is not None:
            if not source._live.is_set():
                raise DurableJobLeaseError("workflow_legacy_original_producer_unavailable")
            await _begin_legacy_aware_writer(db)
            from src.memory.header_bounds import WRS_BY_RUN
            await _legacy_writer_budget(db, source).certify(db, WRS_BY_RUN, (job_id,))
        else:
            # A restart has no private issuer. Inspect only scalar headers to
            # identify the fixed null-fence parent shape before any body read;
            # the bounded body is provenance, never a replacement issuer.
            recovery_shape = (await db.execute(text(
                "SELECT typeof(record_schema_version)='integer' AND record_schema_version>=2 "
                "AND typeof(capability_version)='text' AND capability_version='workflow-v2' "
                "AND typeof(parent_job_id)='text' AND length(CAST(parent_job_id AS BLOB))>0 "
                "AND parent_fencing_token IS NULL FROM workflow_run_states "
                "WHERE run_identity COLLATE BINARY=:identity LIMIT 2"
            ), {"identity": job_id})).scalars().all()
            if recovery_shape == [1]:
                connection = await db.connection()
                raw = await connection.get_raw_connection()
                if not raw.driver_connection.in_transaction:
                    await db.execute(text("BEGIN IMMEDIATE"))
                from src.memory.header_bounds import WRS_BY_RUN
                from src.workflows.durable_state import _LegacyHeaderBudget
                await _LegacyHeaderBudget().certify(db, WRS_BY_RUN, (job_id,))
                raise DurableJobLeaseError("workflow_legacy_original_producer_unavailable")
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
        if run.github_capacity_closure_json and not allow_closed:
            raise DurableJobLeaseError("GitHub capacity is permanently closed for this job")
        return run

    @staticmethod
    def _session(*, header_budget=None):
        # Resolve dynamically so DB fixtures and migration shims can patch the
        # canonical job runtime session factory without changing production
        # persistence behavior.
        return get_session(**(
            {"header_budget": header_budget} if header_budget is not None else {}))


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
