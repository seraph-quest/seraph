"""Private, finite original-owner candidates for the three Memory mutations.

The candidate is data, never authority. Admission/claim/dispatch must retain its
exact bytes on the one original job and publish the effect result in the same
writer. Only opaque references enter the stock service frame.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
import weakref
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import select

from .dispatch import NativeServiceBlocked

MEMORY_JOB_KIND = "runtime_service_memory_v1"
MEMORY_METHODS = frozenset({"memory.propose", "memory.applyReviewed", "memory.forget"})
MEMORY_CONTEXT_TAG = "native-memory-mutation.v1"
MAX_CANDIDATE_BYTES = 16384
METADATA_SOURCE_CAPABILITIES = frozenset({"workflow.goal-snapshot-to-file"})
REPORT_SOURCE_CAPABILITY = "work.local-evidence-report.v1"
_MEMORY_SOURCE_SEAL = object()
_MEMORY_SOURCES = weakref.WeakValueDictionary()
_MEMORY_OPERATIONS = weakref.WeakValueDictionary()
_MEMORY_AUTHORITY_CAPACITY = ContextVar("native_memory_authority_capacity", default=None)
_MEMORY_AUTHORITY_OWNERS = weakref.WeakValueDictionary()
_MEMORY_AUTHORITY_TRACE_ACTIVE = ContextVar("native_memory_authority_trace", default=None)
_MEMORY_AUTHORITY_TRACE_COLLECTORS = weakref.WeakValueDictionary()


class _MemoryAuthorityTraceCollector:
    """Only the original callback's paid numerical debit observations."""
    def __init__(self, owner, prior):
        self.owner = owner
        self.prior = prior
        self.entries = []

    def _checked(self, budget):
        if (_MEMORY_AUTHORITY_TRACE_COLLECTORS.get(id(self)) is not self
                or _MEMORY_AUTHORITY_TRACE_ACTIVE.get() is not self
                or _native_memory_authority_capacity_checked() is not self.owner
                or self.owner.budget is not budget):
            from src.memory.header_bounds import HeaderBoundsError
            raise HeaderBoundsError("memory_authority_numeric_trace_changed")

    def _storage_debit(self, upper, stage):
        from src.memory.header_bounds import _MEMORY_CHARGE_TRACE
        token = _MEMORY_CHARGE_TRACE.set(self.prior)
        try:
            self.owner.budget.debit(upper, appearance=(
                "native-memory-authority-trace", stage, len(self.entries)))
        finally:
            _MEMORY_CHARGE_TRACE.reset(token)

    def append_charge(self, appearance, amount):
        from src.memory.header_bounds import HeaderBoundsError
        from src.memory.composition_headers import _common_trace_label_width
        self._checked(self.owner.budget)
        if type(amount) is not int or not 0 <= amount < 2**63:
            raise HeaderBoundsError("memory_authority_numeric_trace_changed")
        pair = 3 + _common_trace_label_width(appearance) + 20
        self._storage_debit(pair, "entry-pair")
        self._storage_debit(pair + 1, "entry-list-slot")
        entry = (appearance, amount)
        self.entries.append(entry)
        if self.prior is not None and self.prior[0] is self.owner.budget:
            from src.memory.composition_headers import _CommonCertificationTraceCollector
            outer = self.prior[1]
            if type(outer) is _CommonCertificationTraceCollector:
                outer.append_charge(appearance, amount)
            else:
                outer.append(entry)


@dataclass(eq=False)
class _MemoryAuthorityCapacity:
    source: object
    scope: object
    db: object
    transaction: object
    driver: object
    task: object
    budget: object
    runtime_certificate: object = None
    principal_certificate: object = None


@dataclass(eq=False)
class _MemoryCompletionAuthorityCapacity(_MemoryAuthorityCapacity):
    """Original completion resource ownership, never an effect callback."""
    ledger: object = None


@asynccontextmanager
async def _memory_completion_authority_capacity(source, db):
    """Enroll existing owner allocation hooks in ONE original claim check."""
    import asyncio
    from src.memory.header_bounds import (
        HeaderBoundsError, _MEMORY_LEDGER_PHASE, _native_memory_object_width)
    if type(source) is not _NativeMemoryDispatchSource:
        raise HeaderBoundsError("memory_completion_authority_owner_changed")
    source = _validate_memory_completion_capacity_source(source, source.claim)
    ledger = source._retention.ledger
    ledger._checked()
    binding = ledger.bindings.get("completion")
    if (_MEMORY_AUTHORITY_CAPACITY.get() is not None
            or _MEMORY_LEDGER_PHASE.get() != (ledger, "completion")
            or type(binding) is not tuple or len(binding) != 6
            or binding[0] is not db or binding[1] is not db.sync_session.get_transaction()
            or not binding[2].in_transaction or binding[5] is not asyncio.current_task()
            or ledger._completion_capacity_task is not asyncio.current_task()):
        raise HeaderBoundsError("memory_completion_authority_owner_changed")
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    if raw.driver_connection is not binding[2]:
        raise HeaderBoundsError("memory_completion_authority_owner_changed")
    ledger.budget.debit(_native_memory_object_width({name: 4 for name in (
        "source", "scope", "db", "transaction", "driver", "task", "budget",
        "runtime_certificate", "principal_certificate", "ledger")}),
        appearance=("native-memory-completion-authority", "capacity-owner"))
    owner = _MemoryCompletionAuthorityCapacity(source, source._scope, db,
        binding[1], binding[2], binding[5], ledger.budget, ledger=ledger)
    _MEMORY_AUTHORITY_OWNERS[id(owner)] = owner
    token = _MEMORY_AUTHORITY_CAPACITY.set(owner)
    try:
        _native_memory_authority_capacity_checked(db)
        yield owner
    finally:
        _MEMORY_AUTHORITY_CAPACITY.reset(token)
        _MEMORY_AUTHORITY_OWNERS.pop(id(owner), None)


def _native_memory_authority_capacity_checked(db=None):
    """An original callback's resource payer, never body or execution authority."""
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
    from src.workspace.accounting_witness import CompositionSessionGuard
    owner = _MEMORY_AUTHORITY_CAPACITY.get()
    if owner is None:
        return None
    if (type(owner) not in {_MemoryAuthorityCapacity, _MemoryCompletionAuthorityCapacity}
            or _MEMORY_AUTHORITY_OWNERS.get(id(owner)) is not owner):
        raise HeaderBoundsError("memory_authority_numeric_owner_changed")
    source = owner.source
    guard = owner.db.info.get("composition_guard")
    if (type(source) is not _NativeMemoryDispatchSource
            or _MEMORY_SOURCES.get(id(source)) is not source
            or source._seal is not _MEMORY_SOURCE_SEAL or not source._live.is_set()
            or source._scope is not owner.scope or owner.scope.native_memory_source is not source
            or owner.task is not asyncio.current_task() or (db is not None and db is not owner.db)
            or owner.transaction is not owner.db.sync_session.get_transaction()
            or not owner.driver.in_transaction or type(owner.budget) is not HeaderReadBudget
            or owner.budget is not source.admission.header_budget
            or type(guard) is not CompositionSessionGuard or guard.db is not owner.db
            or guard.header_budget is not owner.budget):
        raise HeaderBoundsError("memory_authority_numeric_owner_changed")
    if type(owner) is _MemoryCompletionAuthorityCapacity:
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
        _validate_memory_completion_capacity_source(source, source.claim)
        ledger = source._retention.ledger
        ledger._checked()
        binding = ledger.bindings.get("completion")
        if (owner.ledger is not ledger or _MEMORY_LEDGER_PHASE.get() != (ledger, "completion")
                or binding is None or binding[0] is not owner.db
                or binding[1] is not owner.transaction or binding[2] is not owner.driver
                or binding[5] is not owner.task
                or getattr(guard, "_native_memory_source_retention", None) is not source._retention
                or ledger.budget is not owner.budget
                or ledger._completion_capacity_task is not owner.task):
            raise HeaderBoundsError("memory_completion_authority_owner_changed")
    return owner


def _native_memory_authority_entries(stage, upper, *, dependencies=0, row_uppers=()):
    """Closed allocation occurrences of the original callback's child owners."""
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_object_width
    upper = _native_memory_source_capacity(upper)
    if stage == "inventory":
        if type(row_uppers) is not tuple or len(row_uppers) != 14:
            raise HeaderBoundsError("memory_authority_numeric_inventory_changed")
        aggregate = upper + 2 + 13
        return tuple((("native-memory-authority", name), amount) for name, amount in (
            ("inventory-query", aggregate), ("inventory-list", aggregate),
            ("inventory-domain-set", aggregate), ("inventory-required-set", aggregate),
            ("inventory-dictionary", aggregate))) + tuple(
            (("native-memory-authority", "inventory-dependency"), _native_memory_source_capacity(row_upper))
            for row_upper in row_uppers)
    if stage == "dependencies":
        return tuple((("native-memory-authority", "binding-dependency"), upper)
            for _ in range(dependencies))
    if stage == "principal-query":
        return ((("native-memory-authority", "principal-query"), upper),
            (("native-memory-authority", "principal-materialization"), upper),
            (("native-memory-authority", "principal-now"), 44),
            (("native-memory-authority", "principal-idle-aware"), 44),
            (("native-memory-authority", "principal-absolute-aware"), 44))
    if stage == "reverse-results":
        ids = 3 * (6 * 512 + 2) + 2
        return tuple((("native-memory-authority", name), ids) for name in (
            "reverse-nontombstone-query", "reverse-scalars", "reverse-list",
            "reverse-tombstone-query", "reverse-ownership-tuple"))
    if stage == "operator-constructor":
        # Original _principal repeats the paid Root id twice and principal id
        # once; four exact enum values, all eight dataclass fields/defaults.
        from src.security.trust_contract import AuthorityGrant
        grants = 2 + 3 + sum(_native_memory_source_value_upper(grant.value) for grant in (
            AuthorityGrant.INGRESS, AuthorityGrant.MODEL_INFERENCE,
            AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.ARTIFACT_TRANSFER))
        principal = _native_memory_object_width({"principal_id": upper, "principal_type": 10,
            "authenticated": 4, "revoked": 5, "grants": grants, "session_id": upper,
            "job_id": 2, "operator_session_id": upper})
        operator = _native_memory_object_width({"session_id": upper, "principal": principal,
            "idle_expires_at": 44, "absolute_expires_at": 44, "_token_hash": 4,
            "ownership_continuity": 6 * len("legacy_rebind_required") + 2,
            "ownership_recovery_action": 6 * len("review_and_recreate_work_in_current_scope") + 2,
            "operator_identity_id": upper, "_continuity_token": 4})
        return tuple((("native-memory-authority", name), amount) for name, amount in (
            ("principal-grant-tuple", grants), ("principal-constructor", principal),
            ("operator-idle-aware", 44), ("operator-absolute-aware", 44),
            ("operator-constructor", operator)))
    raise HeaderBoundsError("memory_authority_numeric_stage_changed")


def _native_memory_authority_entry_construction_upper(stage, *, dependencies=0):
    """Closed original child-vector constructors, excluding original body fees."""
    from src.memory.header_bounds import HeaderBoundsError
    if type(dependencies) is not int or not 0 <= dependencies <= 34:
        raise HeaderBoundsError("memory_authority_construction_shape_unavailable")
    if stage == "inventory":
        count = 19
        # Five-prefix and fourteen-dependency tuples, followed by joined19.
        intermediates = 5 + 5 + 14  # literal input5, prefix output5, dependency output14
        frames = 2
    elif stage == "dependencies":
        count = dependencies
        intermediates = 0
        frames = 1
    elif stage == "principal-query":
        count = 5
        intermediates = 0
        frames = 0
    elif stage == "reverse-results":
        count = 5
        intermediates = 0
        frames = 1
    elif stage == "operator-constructor":
        count = 5
        intermediates = 5  # original literal input pairs before tuple generator
        frames = 25  # four grant strings/sum + two map sums/seventeen key strings + return generator
    else:
        raise HeaderBoundsError("memory_authority_construction_shape_unavailable")
    label = 3 + len("native-memory-authority") + 2 + 40 + 2
    pair = 3 + label + 20
    vector = 2 + max(0, count - 1) + count * pair
    upper = vector + count * (pair + label) + frames * 41
    if intermediates:
        upper += 4 + intermediates - 1 + intermediates * pair
    if stage == "operator-constructor":
        # Actual eight-field principal and nine-field operator numerical maps.
        upper += 2 + 7 + 8 * (6 * 40 + 3 + 20)
        upper += 2 + 8 + 9 * (6 * 40 + 3 + 20)
    return upper


def _native_memory_authority_trace_label_upper(value):
    """Nonallocating capacity width of an already-issued numerical label."""
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_utf8_width
    if type(value) is str:
        return 6 * _native_memory_utf8_width(value) + 2
    if type(value) is int:
        return 20
    if type(value) is bool:
        return 5
    if value is None:
        return 4
    if type(value) is tuple:
        upper = 2 + max(0, len(value) - 1)
        for child in value:
            upper += _native_memory_authority_trace_label_upper(child)
        return upper
    raise HeaderBoundsError("memory_authority_numeric_trace_changed")


def _native_memory_authority_retained_trace_owner(operation, certificate):
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError
    from .ownership import DOMAINS
    retained = getattr(operation.db.info["composition_guard"], "_native_memory_authority_read_numeric", None)
    if (type(retained) is not tuple or len(retained) != 10
            or retained[0] is not operation.source or retained[1] is not operation.scope
            or retained[2] is not operation.db or retained[3] is not operation.transaction
            or retained[4] is not operation.driver or retained[5] is not asyncio.current_task()
            or retained[6] is not certificate.budget or type(retained[7]) is not tuple
            or type(retained[8]) is not tuple or len(retained[8]) != len(DOMAINS)
            or type(retained[9]) is not tuple):
        raise HeaderBoundsError("memory_authority_numeric_trace_changed")
    for index in range(len(DOMAINS)):
        if retained[8][index] != DOMAINS[index]:
            raise HeaderBoundsError("memory_authority_numeric_trace_changed")
    roots = 0
    for table, key in certificate.rows:
        if table == "operator_sessions":
            roots += 1
            if retained[9].count(key) != 1:
                raise HeaderBoundsError("memory_authority_numeric_trace_changed")
    if len(retained[9]) != roots:
        raise HeaderBoundsError("memory_authority_numeric_trace_changed")
    return retained


def original_native_memory_authority_construction_numeric_upper(operation, prepared, *,
        certificate, facts, retention_widths, kind="dispatch"):
    """ONE pure authority schedule constructor; parent retention is separate."""
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    from src.workflows.job_runtime import _native_memory_completion_bound_input_construction_upper
    from src.memory.header_bounds import HeaderBoundsError
    if kind != "dispatch" and kind != "completion-child":
        raise HeaderBoundsError("memory_authority_construction_kind_unavailable")
    if operation.run.owner_kind != "user":
        raise HeaderBoundsError("memory_authority_numeric_shape_unavailable")
    dependencies = len(operation.scope.binding.dependency_vector)
    if not 0 <= dependencies <= 34:
        raise HeaderBoundsError("memory_authority_construction_shape_unavailable")
    core = _native_memory_completion_bound_input_construction_upper(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    runtime_count = root_count = 0
    for table, key in certificate.rows:
        if table == "runtime_composition_states":
            runtime_count += 1
        if table == "operator_sessions":
            root_count += 1
    if runtime_count != 14 or root_count == 0:
        raise HeaderBoundsError("memory_authority_construction_shape_unavailable")
    # Every entry is a pair of an ASCII appearance tuple (<=three40-char
    # components) and an i64 upper, never a body/model byte substitute.
    label = 4 + 3 * 42
    pair = 3 + label + 20
    child_count = 35 + dependencies
    count = child_count + 5  # five source-owned runtime constructor fees
    if kind == "dispatch":
        count += 21 + (4 if operation.run.goal_id is not None else 0)
    # Exact default result concatenations: child inventories19, dependencies,
    # three five-vectors, owner; dispatch witness11/scalars7/grants3/Goal4.
    # Eleven result tuples include the initial witness output and all ten
    # dispatch concatenations; completion has fewer closed result tuples.
    container = 2 + count - 1 + count * pair
    result = core + 11 * container + 2 * count * (pair + label)
    result += 2 + 13 + 14 * 20 + 2 + root_count - 1 + root_count * 20
    projection = operation.db.info["composition_guard"]._native_memory_source_projection
    projection_count = len(projection[1]) + len(projection[2])
    result += 2 + max(0, projection_count - 1) + projection_count * 15
    result += projection_count * (2 + 3 * 4)  # original per-row closed-table set
    result += 10 * 41  # runtime/root/projection and original result generators
    for stage in ("inventory", "dependencies", "principal-query", "reverse-results", "operator-constructor"):
        result += _native_memory_authority_entry_construction_upper(stage, dependencies=dependencies)
    # Source capacity-owner numeric map: ten literal fields, per-key generator.
    result += 2 + 9 + 10 * (6 * 40 + 3 + 4) + 11 * 41
    if kind == "dispatch":
        from src.memory.m5 import _native_prepared_width_planning_scratch
        for name, value in operation.scope.witness.items():
            result += _native_prepared_width_planning_scratch(name)
            result += _native_prepared_width_planning_scratch(value)
        result += 41 + 2 * (2 + 10 + 11 * pair)  # witness sum/input/returned11
        result += 5 * 41  # grant sum and four original closed string widths
        # Combined read trace: label set, two equality sets, roster/map,
        # read tuple and final concatenated tuple, all retained actual rows.
        retained = _native_memory_authority_retained_trace_owner(operation, certificate)
        reads = len(retained[7])
        read_labels = 0
        for appearance, amount in retained[7]:
            read_labels += _native_memory_authority_trace_label_upper(appearance)
        result += 2 * (2 + reads - 1 + read_labels + reads * 23)
        result += 2 + reads + count - 1 + read_labels + reads * 23 + count * pair
        result += 6 * 41
        result += 3 * (2 + count - 1 + count * (label + 1 + 3 + 20))
    return result


def _native_memory_authority_owner_pay(db, stage, *, certificate=None, binding=None, record=None):
    owner = _native_memory_authority_capacity_checked(db)
    if owner is None:
        return
    from src.memory.header_bounds import HeaderBoundsError, HeaderCertificate, RUNTIME_COMPOSITION, OPERATOR_SESSION, _SEAL
    if stage in {"inventory", "principal-certificate"}:
        descriptor = RUNTIME_COMPOSITION if stage == "inventory" else OPERATOR_SESSION
        if (type(certificate) is not HeaderCertificate or certificate._seal is not _SEAL
                or certificate._issued_id != id(certificate) or certificate.descriptor is not descriptor
                or certificate._db is not owner.db or certificate._transaction is not owner.transaction
                or certificate._driver is not owner.driver):
            raise HeaderBoundsError("memory_authority_numeric_certificate_changed")
        if stage == "inventory":
            owner.runtime_certificate = certificate
        else:
            owner.principal_certificate = certificate
            return
    else:
        certificate = owner.runtime_certificate if stage == "dependencies" else owner.principal_certificate
        if certificate is None:
            raise HeaderBoundsError("memory_authority_numeric_certificate_missing")
    if stage == "dependencies":
        if type(owner) is _MemoryCompletionAuthorityCapacity:
            from .ownership import RuntimeCompositionBinding
            if type(binding) is not RuntimeCompositionBinding or binding != owner.source.claim.binding:
                raise HeaderBoundsError("memory_authority_numeric_binding_changed")
        elif binding is not owner.scope.binding:
            raise HeaderBoundsError("memory_authority_numeric_binding_changed")
    if record is not None and (record.id not in certificate.row_ids
            or record.principal_id != owner.source.operator.principal.principal_id):
        raise HeaderBoundsError("memory_authority_numeric_principal_changed")
    dependencies = len(binding.dependency_vector) if binding is not None else 0
    owner.budget.debit(_native_memory_authority_entry_construction_upper(stage, dependencies=dependencies),
        appearance=("native-memory-authority", "child-construction", stage))
    for appearance, upper in _native_memory_authority_entries(stage, certificate.upper_bytes,
            dependencies=dependencies, row_uppers=certificate.row_upper_bytes):
        owner.budget.debit(upper, appearance=appearance)


def original_native_memory_dispatch_authority_allocation_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE future callback's allocations, not its original certification fees.

    The complete composer must independently retain the original initial
    callback's metadata/body trace. This helper cannot substitute row uppers
    for schema/compiler/certificate mechanics or issue an effect ledger.
    """
    from src.workspace.accounting_witness import _native_memory_generic_future_bound_inputs
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_object_width
    from .ownership import DOMAINS
    _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, retention_widths)
    run = operation.run
    if run.owner_kind != "user":
        raise HeaderBoundsError("memory_authority_numeric_shape_unavailable")
    runtime = tuple(certificate.rows[("runtime_composition_states", domain)][1] for domain in DOMAINS)
    roots = tuple(cost for (table, key), (rowid, cost) in certificate.rows.items()
        if table == "operator_sessions")
    root = certificate.rows[("operator_sessions", run.operator_session_id)][1]
    if len(runtime) != 14 or not roots:
        raise HeaderBoundsError("memory_authority_numeric_shape_unavailable")
    projection = operation.db.info["composition_guard"]._native_memory_source_projection
    if any(descriptor.table in {"runtime_composition_states", "operator_sessions", "goals"}
            for descriptor, key, row in (*projection[1], *projection[2])):
        raise HeaderBoundsError("memory_authority_numeric_shape_unavailable")
    journal = max(facts["text_bytes"][("workflow_run_states", run.run_identity)]["checkpoint_receipts_json"],
        retention_widths["future_text_bytes"]["checkpoint_receipts_json"])
    witness = _native_memory_dispatch_witness_upper(operation.scope.witness)
    result = tuple((("native-memory-authority", stage), upper)
        for stage, upper in _native_memory_dispatch_witness_entries(journal, witness))
    result += tuple((("native-memory-authority", stage), upper) for stage, upper in (
        ("scope-witness-copy", witness), ("deadline-replace", 44),
        ("deadline-astimezone", 44), ("deadline-now", 44), ("root-now", 44),
        ("root-query", root), ("root-materialization", root)))
    result += _native_memory_authority_entries("inventory", sum(runtime), row_uppers=runtime)
    result += _native_memory_authority_entries("dependencies", sum(runtime),
        dependencies=len(operation.scope.binding.dependency_vector))
    for stage in ("principal-query", "reverse-results", "operator-constructor"):
        result += _native_memory_authority_entries(stage, sum(roots))
    from src.security.trust_contract import AuthorityGrant
    grants = 2 + 3 + sum(_native_memory_source_value_upper(grant.value) for grant in (
        AuthorityGrant.INGRESS, AuthorityGrant.MODEL_INFERENCE,
        AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.ARTIFACT_TRANSFER))
    authority = 6 * facts["text_bytes"][("workflow_run_states", run.run_identity)]["declared_authority_json"] + 2
    result += tuple((("native-memory-authority", stage), upper) for stage, upper in (
        ("authority-json-parser", authority), ("grant-generator", grants), ("grant-sort-list", grants)))
    if run.goal_id is not None:
        goal = certificate.rows[("goals", run.goal_id)][1]
        result += tuple((("native-memory-authority", stage), upper) for stage, upper in (
            ("goal-query", goal), ("goal-materialization", goal),
            ("goal-owner-session-tuple", goal), ("goal-authority-json-parser", authority)))
    result += ((("native-memory-authority", "capacity-owner"), _native_memory_object_width(
        {name: 4 for name in ("source", "scope", "db", "transaction", "driver", "task", "budget",
            "runtime_certificate", "principal_certificate")})),)
    result += tuple((("native-memory-authority", "child-construction", stage),
        _native_memory_authority_entry_construction_upper(stage,
            dependencies=len(operation.scope.binding.dependency_vector)))
        for stage in ("inventory", "dependencies", "principal-query", "reverse-results", "operator-constructor"))
    return result



def original_native_memory_completion_authority_allocation_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE original completion claim's child-owner allocations only.

    Claim request, verifier parsing/Root lookup/Goal fence and the original
    claim's ordered certification trace belong to their existing job owner.
    This vector neither reuses callback metadata nor issues capacity/authority.
    """
    from src.workspace.accounting_witness import _native_memory_generic_future_bound_inputs
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_object_width
    from .ownership import DOMAINS
    _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, retention_widths)
    if operation.run.owner_kind != "user":
        raise HeaderBoundsError("memory_completion_authority_shape_unavailable")
    projection = operation.db.info["composition_guard"]._native_memory_source_projection
    if any(descriptor.table in {"runtime_composition_states", "operator_sessions"}
            for descriptor, key, row in (*projection[1], *projection[2])):
        raise HeaderBoundsError("memory_completion_authority_shape_unavailable")
    runtime = tuple(certificate.rows[("runtime_composition_states", domain)][1] for domain in DOMAINS)
    roots = tuple(cost for (table, key), (rowid, cost) in certificate.rows.items()
        if table == "operator_sessions")
    if len(runtime) != 14 or not roots:
        raise HeaderBoundsError("memory_completion_authority_shape_unavailable")
    result = _native_memory_authority_entries("inventory", sum(runtime), row_uppers=runtime)
    result += _native_memory_authority_entries("dependencies", sum(runtime),
        dependencies=len(operation.scope.binding.dependency_vector))
    for stage in ("principal-query", "reverse-results", "operator-constructor"):
        result += _native_memory_authority_entries(stage, sum(roots))
    result += tuple((("native-memory-authority", "child-construction", stage),
        _native_memory_authority_entry_construction_upper(stage,
            dependencies=len(operation.scope.binding.dependency_vector)))
        for stage in ("inventory", "dependencies", "principal-query", "reverse-results", "operator-constructor"))
    return ((("native-memory-completion-authority", "capacity-owner"),
        _native_memory_object_width({name: 4 for name in ("source", "scope", "db", "transaction",
            "driver", "task", "budget", "runtime_certificate", "principal_certificate", "ledger")})),) + result


def _native_memory_authority_filter_prepaid_trace(trace, allocations):
    """FIFO occurrence/amount match; caller prepays this closed construction."""
    from src.memory.header_bounds import HeaderBoundsError
    amounts = {}
    indices = {}
    for appearance, upper in allocations:
        if type(upper) is not int or not 0 <= upper < 2**63:
            raise HeaderBoundsError("memory_authority_numeric_trace_changed")
        if appearance not in amounts:
            amounts[appearance] = []
            indices[appearance] = 0
        amounts[appearance].append(upper)
    reads = []
    for appearance, upper in trace:
        if type(upper) is not int or not 0 <= upper < 2**63:
            raise HeaderBoundsError("memory_authority_numeric_trace_changed")
        if appearance in amounts:
            index = indices[appearance]
            slots = amounts[appearance]
            if index >= len(slots) or upper > slots[index]:
                raise HeaderBoundsError("memory_authority_numeric_paid_amount_exceeded")
            indices[appearance] = index + 1
        else:
            reads.append((appearance, upper))
    if not reads:
        raise HeaderBoundsError("memory_authority_numeric_trace_changed")
    return tuple(reads)


def original_native_memory_dispatch_authority_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE real callback's original read fees plus closed future allocations.

    Rephase this vector twice into effect and once into capture. Its single
    initial observation and collector storage were already spent; they are
    never replayed, refunded, or treated as three new callback observations.
    """
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError
    from .ownership import DOMAINS
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    retained = _native_memory_authority_retained_trace_owner(operation, certificate)
    allocations = original_native_memory_dispatch_authority_allocation_numeric_schedule(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=retention_widths)
    original_reads = _native_memory_authority_filter_prepaid_trace(retained[7], allocations)
    return original_reads + allocations


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MemorySourceBinding(_Closed):
    task_id: str = Field(min_length=1, max_length=512)
    expected_task_revision: int = Field(ge=1)
    attempt_id: str = Field(min_length=1, max_length=512)
    attempt_fence: int = Field(ge=1)
    workflow_run_id: str = Field(min_length=1, max_length=512)
    workflow_run_revision: int = Field(ge=0)
    goal_id: str = Field(min_length=1, max_length=512)
    goal_revision: int = Field(ge=1)
    capability_id: str = Field(min_length=1, max_length=512)
    capability_version: str = Field(min_length=1, max_length=128)
    typed_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    readback_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _PreparedText(_Closed):
    original_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_text: str = Field(min_length=1, max_length=2000)
    vault_rows_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _Candidate(_Closed):
    schema_version: Literal[1]
    operator_principal_id: str = Field(min_length=1, max_length=512)
    operator_session_id: str = Field(min_length=1, max_length=512)
    opaque_ref: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    idempotency_key: str = Field(pattern=r"^[\x21-\x7e]{1,128}$")
    original_deadline: str
    host_boot_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    composition_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def deadline(self):
        parsed = datetime.fromisoformat(self.original_deadline)
        if parsed.tzinfo != timezone.utc or parsed.isoformat() != self.original_deadline:
            raise ValueError("original deadline must be canonical UTC")
        return self


class _Propose(_Candidate):
    method: Literal["memory.propose"]
    source: MemorySourceBinding
    prepared_text: _PreparedText


class _Review(_Candidate):
    method: Literal["memory.applyReviewed"]
    source: MemorySourceBinding
    proposal_id: str = Field(min_length=1, max_length=512)
    proposal_schema: Literal["memory_proposal.v1"]
    action: Literal["accept", "edit_accept"]
    expected_revision: int = Field(ge=1)
    expected_preview_text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    edited_text: str | None = Field(max_length=2000)
    decision_effect: Literal["none", "require_operator_confirmation"] | None
    preferred_capability_id: str | None = Field(max_length=160)
    corrects_memory_id: str | None = Field(max_length=255)
    reason: str | None = Field(max_length=500)
    proposal_expires_at: str
    prepared_text: _PreparedText

    @model_validator(mode="after")
    def review_shape(self):
        if self.action == "accept" and self.edited_text is not None:
            raise ValueError("edited text requires edit_accept")
        expires = datetime.fromisoformat(self.proposal_expires_at)
        if (expires.tzinfo != timezone.utc or expires.isoformat() != self.proposal_expires_at
            or datetime.fromisoformat(self.original_deadline) > expires):
            raise ValueError("review deadline exceeds original expiry")
        return self


class _Forget(_Candidate):
    method: Literal["memory.forget"]
    record_ref: str = Field(min_length=1, max_length=128)
    mode: Literal["archive", "redact"]
    privacy_boundary: Literal["operator_visible", "private", "sensitive", "source_bound"]
    reason: str | None = Field(max_length=500)
    prepared_reason: _PreparedText | None

    @model_validator(mode="after")
    def reason_binding(self):
        if (self.reason is None) != (self.prepared_reason is None):
            raise ValueError("forget reason requires its original Vault staging")
        return self


_CANDIDATE = TypeAdapter(_Propose | _Review | _Forget)


def _native_memory_source_numeric_frame():
    # Reuse the existing exact ledger/current-budget resource payer. It is not
    # Source, key, body-read or mutation authority.
    from src.extensions.capability_execution import _configured_mac_numeric_frame
    return _configured_mac_numeric_frame()


def _native_memory_source_value_upper(value):
    from src.workspace.accounting_witness import _native_memory_closed_value_width
    return _native_memory_closed_value_width(value)


def _native_memory_source_model_upper(model):
    """Original closed typed fields/defaults, without model_dump or copying."""
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if type(model) not in {_Propose, _Review, _Forget, MemorySourceBinding, _PreparedText}:
        raise HeaderBoundsError("memory_source_numeric_model_unavailable")
    fields = type(model).model_fields
    def width(value):
        return (_native_memory_source_model_upper(value) if isinstance(value, BaseModel)
            else _native_memory_source_value_upper(value))
    return 2 + max(0, len(fields) - 1) + sum(
        _native_memory_ascii_string_width(name) + 1 + width(getattr(model, name))
        for name in fields)


def _native_memory_source_pay(owner, stage, upper):
    frame = _native_memory_source_numeric_frame()
    if frame is not None:
        frame.debit(upper, appearance=("native-memory-source", owner, stage))


def _native_memory_source_capacity(upper):
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    if type(upper) is not int or not 0 <= upper <= MAX_BYTES:
        raise HeaderBoundsError("memory_source_numeric_capacity_unavailable")
    return upper


def _native_memory_source_validation_entries(candidate_upper):
    return tuple((("native-memory-source", "candidate-validation", stage), candidate_upper)
        for stage in ("type-adapter", "model-dump", "canonical", "utf8"))


def _native_memory_source_admission_entries(candidate_upper):
    from src.memory.header_bounds import _native_memory_object_width
    return (_native_memory_source_validation_entries(candidate_upper)
        + tuple((("native-memory-source", "from-candidate", stage), candidate_upper)
            for stage in ("canonical", "digest-canonical", "digest-utf8"))
        + ((("native-memory-source", "from-candidate", "digest-return"), 66),
            (("native-memory-source", "from-candidate", "admission-construction"),
                _native_memory_object_width({"candidate_json": candidate_upper,
                    "candidate_digest": 66, "report_source": 4, "header_budget": 4}))))


def original_native_memory_source_candidate_numeric_entries(*, candidate_upper):
    """One original Admission.candidate call, including its real validation."""
    candidate_upper = _native_memory_source_capacity(candidate_upper)
    return ((("native-memory-source", "candidate", "json-parser"), candidate_upper),
        *_native_memory_source_validation_entries(candidate_upper),
        *tuple((("native-memory-source", "candidate", stage), candidate_upper)
            for stage in ("comparison-canonical", "digest-canonical", "digest-utf8")),
        (("native-memory-source", "candidate", "digest-return"), 66))


def _native_memory_source_binding_entries(binding_text_bytes, *, current_digest=False):
    from src.workflows.inference_accounting import _original_native_binding_numeric_entries
    entries = _original_native_binding_numeric_entries(None, binding_text_bytes)
    if current_digest:
        names = {"payload-methods-list", "payload-dependencies-list", "binding-payload",
            "binding-canonical", "binding-encoded", "binding-digest", "dependency-payloads"}
        entries = tuple((appearance, upper) for appearance, upper in entries
            if appearance[2] in names or (type(appearance[2]) is tuple
                and appearance[2][:2] == ("manifest", 1)))
    owner = "context-binding-digest" if current_digest else "context-binding-parser"
    return tuple((("native-memory-source", owner, appearance[2]), upper)
        for appearance, upper in entries)


def _native_memory_source_field_set(value, owner, stage):
    if _native_memory_source_numeric_frame() is not None:
        upper = _native_memory_source_value_upper(value)
        _native_memory_source_pay(owner, stage, upper)
        _native_memory_source_pay(owner, (stage, "expected"), upper)
    return set(value)


def _native_memory_source_digest(value, owner, stage):
    if _native_memory_source_numeric_frame() is None:
        return _digest(value)
    from src.memory.header_bounds import _native_memory_utf8_width
    _native_memory_source_pay(owner, stage + "-canonical", _native_memory_source_value_upper(value))
    canonical = _canonical(value)
    _native_memory_source_pay(owner, stage + "-utf8", _native_memory_utf8_width(canonical))
    encoded = canonical.encode("utf-8")
    _native_memory_source_pay(owner, stage + "-return", 66)
    return hashlib.sha256(encoded).hexdigest()


def _native_memory_source_binding_digest(binding, text):
    frame = _native_memory_source_numeric_frame()
    if frame is not None:
        from src.memory.header_bounds import _native_memory_utf8_width
        for appearance, upper in _native_memory_source_binding_entries(
                _native_memory_utf8_width(text), current_digest=True):
            frame.debit(upper, appearance=appearance)
    return binding.binding_digest


def _native_memory_source_deadline_iso(value):
    if _native_memory_source_numeric_frame() is not None:
        for stage in ("deadline-replace", "deadline-astimezone", "deadline-iso"):
            _native_memory_source_pay("context", stage, 44)
    return _utc(value).isoformat()


def original_native_memory_source_context_numeric_entries(*, context_upper,
        candidate_upper, binding_text_bytes, result_upper, effect_upper,
        locator_upper, result_present):
    """One original memory_context call; nested candidate calls are included."""
    for upper in (context_upper, candidate_upper, binding_text_bytes,
            result_upper, effect_upper, locator_upper):
        _native_memory_source_capacity(upper)
    if type(result_present) is not bool:
        from src.memory.header_bounds import HeaderBoundsError
        raise HeaderBoundsError("memory_source_numeric_capacity_unavailable")
    entries = ((("native-memory-source", "context", "size-utf8"), max(0, (context_upper - 2) // 6)),
        *tuple((("native-memory-source", "context", stage), context_upper)
            for stage in ("json-parser", "duplicate-pairs", "duplicate-dicts", "field-set", ("field-set", "expected"))),
        *_native_memory_source_admission_entries(candidate_upper),
        *original_native_memory_source_candidate_numeric_entries(candidate_upper=candidate_upper),
        *_native_memory_source_binding_entries(binding_text_bytes),
        *_native_memory_source_binding_entries(binding_text_bytes, current_digest=True),
        *tuple((("native-memory-source", "context", stage), 44)
            for stage in ("deadline-replace", "deadline-astimezone", "deadline-iso")))
    if result_present:
        entries += ((("native-memory-source", "context", "result-field-set"), context_upper),
            (("native-memory-source", "context", ("result-field-set", "expected")), context_upper),
            *tuple((("native-memory-source", "context", stage), result_upper)
                for stage in ("result-digest-canonical", "result-digest-utf8")),
            (("native-memory-source", "context", "result-digest-return"), 66),
            (("native-memory-source", "context", "result-validation"), result_upper),
            (("native-memory-source", "context", "result-validation-fields"), result_upper),
            (("native-memory-source", "context", "result-validation-value-fields"), result_upper),
            (("native-memory-source", "context", "effect-keywords"), effect_upper),
            (("native-memory-source", "context", "effect-construction"), effect_upper),
            (("native-memory-source", "retention-locator", "field-set"), locator_upper),
            (("native-memory-source", "retention-locator", ("field-set", "expected")), locator_upper))
    return entries


def validate_memory_candidate(value):
    try:
        if type(value) is not dict or type(value.get("schema_version")) is not int:
            raise ValueError("closed JSON candidate required")
        guarded = _native_memory_source_numeric_frame() is not None
        if guarded:
            _native_memory_source_pay("candidate-validation", "type-adapter",
                _native_memory_source_value_upper(value))
        model = _CANDIDATE.validate_python(value)
        if guarded:
            # Size its actual typed fields/defaults before their JSON copy.
            _native_memory_source_pay("candidate-validation", "model-dump",
                _native_memory_source_model_upper(model))
        candidate = model.model_dump(mode="json")
        if guarded:
            _native_memory_source_pay("candidate-validation", "canonical",
                _native_memory_source_value_upper(candidate))
        canonical = _canonical(candidate)
        if guarded:
            from src.memory.header_bounds import _native_memory_utf8_width
            _native_memory_source_pay("candidate-validation", "utf8", _native_memory_utf8_width(canonical))
        if len(canonical.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise ValueError("private Memory candidate exceeds UTF-8 bound")
        return candidate
    except (ValueError, TypeError) as exc:
        raise NativeServiceBlocked("native_memory_candidate_invalid") from exc


@dataclass(frozen=True)
class NativeMemoryMutationAdmission:
    candidate_json: str
    candidate_digest: str
    report_source: object = field(default=None, repr=False, compare=False)
    header_budget: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_candidate(cls, value):
        candidate = validate_memory_candidate(value)
        if _native_memory_source_numeric_frame() is None:
            return cls(_canonical(candidate), _digest(candidate))
        upper = _native_memory_source_value_upper(candidate)
        _native_memory_source_pay("from-candidate", "canonical", upper)
        canonical = _canonical(candidate)
        _native_memory_source_pay("from-candidate", "digest-canonical", upper)
        material = _canonical(candidate)
        from src.memory.header_bounds import _native_memory_utf8_width, _native_memory_object_width
        _native_memory_source_pay("from-candidate", "digest-utf8", _native_memory_utf8_width(material))
        encoded = material.encode("utf-8")
        _native_memory_source_pay("from-candidate", "digest-return", 66)
        digest = hashlib.sha256(encoded).hexdigest()
        _native_memory_source_pay("from-candidate", "admission-construction",
            _native_memory_object_width({"candidate_json": _native_memory_source_value_upper(canonical),
                "candidate_digest": 66, "report_source": 4, "header_budget": 4}))
        return cls(canonical, digest)

    def candidate(self):
        try:
            if _native_memory_source_numeric_frame() is not None:
                from src.memory.header_bounds import _native_memory_utf8_width
                _native_memory_source_pay("candidate", "json-parser",
                    6 * _native_memory_utf8_width(self.candidate_json) + 2)
            value = validate_memory_candidate(json.loads(self.candidate_json))
            if _native_memory_source_numeric_frame() is None:
                matches = _canonical(value) == self.candidate_json and _digest(value) == self.candidate_digest
            else:
                upper = _native_memory_source_value_upper(value)
                _native_memory_source_pay("candidate", "comparison-canonical", upper)
                matches = _canonical(value) == self.candidate_json
                if matches:
                    _native_memory_source_pay("candidate", "digest-canonical", upper)
                    material = _canonical(value)
                    _native_memory_source_pay("candidate", "digest-utf8", _native_memory_utf8_width(material))
                    encoded = material.encode("utf-8")
                    _native_memory_source_pay("candidate", "digest-return", 66)
                    matches = hashlib.sha256(encoded).hexdigest() == self.candidate_digest
            if not matches:
                raise ValueError("candidate bytes changed")
            return value
        except (ValueError, TypeError) as exc:
            raise NativeServiceBlocked("native_memory_candidate_changed") from exc

    def wire_inputs(self):
        candidate = self.candidate()
        key = "review_ref" if candidate["method"] == "memory.applyReviewed" else "request_ref"
        return {key: candidate["opaque_ref"]}


@dataclass(frozen=True, eq=False)
class _NativeMemoryDispatchSource:
    """Actual inline claim/candidate/operator objects; never a JSON grant."""
    claim: object = field(repr=False)
    admission: NativeMemoryMutationAdmission = field(repr=False)
    operator: object = field(repr=False)
    host: object = field(repr=False)
    report_source: object = field(repr=False)
    _live: object = field(default_factory=threading.Event, init=False, repr=False)
    _seal: object = field(default=None, init=False, repr=False)
    _issued_id: int = field(default=0, init=False, repr=False)
    _scope: object = field(default=None, init=False, repr=False)
    _owner_task: object = field(default=None, init=False, repr=False)

    def close(self):
        self._live.clear()
        _MEMORY_SOURCES.pop(id(self), None)


def _issue_memory_dispatch_source(claim, admission, operator, host):
    # Only execute_native_memory transports the actual claim return here.
    import asyncio
    owner_task = asyncio.current_task()
    if owner_task is None:
        raise NativeServiceBlocked("native_memory_original_source_task_required")
    from src.auth.service import AuthenticatedOperator
    from src.workflows.job_runtime import NativeServiceClaim
    from .bridge import CordisHost
    from .dispatch import capture_original_scope
    if (type(claim) is not NativeServiceClaim or type(admission) is not NativeMemoryMutationAdmission
            or type(operator) is not AuthenticatedOperator or type(host) is not CordisHost
            or claim._host is not host or not host.admitting or host.reviewed is None):
        raise NativeServiceBlocked("native_memory_actual_claim_source_required")
    candidate = admission.candidate()
    original = capture_original_scope(claim, host)
    witness = dict(original.witness)
    if (witness["origin_method"] != candidate["method"]
            or witness["input_digest"] != admission.candidate_digest
            or witness["composition_binding_digest"] != candidate["composition_binding_digest"]
            or witness["host_boot_nonce"] != candidate["host_boot_nonce"]
            or operator.session_id != candidate["operator_session_id"]
            or operator.principal.principal_id != candidate["operator_principal_id"]):
        raise NativeServiceBlocked("native_memory_actual_claim_source_changed")
    source = _NativeMemoryDispatchSource(claim, admission, operator, host, admission.report_source)
    object.__setattr__(source, "_seal", _MEMORY_SOURCE_SEAL)
    object.__setattr__(source, "_issued_id", id(source))
    source._live.set()
    _MEMORY_SOURCES[id(source)] = source
    scope = replace(original, native_memory_report_source=admission.report_source, native_memory_source=source)
    object.__setattr__(source, "_scope", scope)
    object.__setattr__(source, "_owner_task", owner_task)
    return scope


def memory_dispatch_header_budget(*, dispatcher, scope, called_scope, invocation_ref, method):
    """Return numeric capacity only from the actual unresolved Source call."""
    from .bridge import CordisHost, Pending, cordis_host
    from .dispatch import OriginalServiceInvocation, CalledServiceInvocation, NativeServiceDispatcher
    from src.workflows.job_runtime import NativeServiceClaim
    from src.memory.header_bounds import HeaderReadBudget
    if (method not in MEMORY_METHODS or type(scope) is not OriginalServiceInvocation
            or type(called_scope) is not CalledServiceInvocation or called_scope.original is not scope
            or type(dispatcher) is not NativeServiceDispatcher):
        raise NativeServiceBlocked("native_memory_actual_source_required")
    source = scope.native_memory_source
    if (type(source) is not _NativeMemoryDispatchSource or source._seal is not _MEMORY_SOURCE_SEAL
            or source._issued_id != id(source) or _MEMORY_SOURCES.get(id(source)) is not source
            or not source._live.is_set() or source._scope is not scope
            or source.report_source is not scope.native_memory_report_source
            or type(source.claim) is not NativeServiceClaim or type(source.host) is not CordisHost
            or source.host is not cordis_host or source.claim._host is not source.host
            or source.host.service_dispatch is not dispatcher):
        raise NativeServiceBlocked("native_memory_actual_source_unavailable")
    host, claim = source.host, source.claim
    pending = host._pending.get(called_scope.parent_request_id)
    if (not host.admitting or host.reviewed is None
            or claim.host_boot_nonce != host.boot_nonce or scope.host_boot_nonce != host.boot_nonce
            or called_scope.host_boot_nonce != host.boot_nonce
            or host.reviewed.package_digest != scope.witness["package_digest"]
            or host.reviewed.composition_digest != scope.witness["host_composition_digest"]
            or scope.binding is not claim.binding or scope.binding.origin_method != method
            or scope.witness["origin_method"] != method or called_scope.method != method
            or scope.witness["invocation_ref"] != invocation_ref or claim.job["job_id"] != invocation_ref
            or invocation_ref not in host._native_inflight
            or type(pending) is not Pending or pending.future.done()
            or pending.native_binding is not called_scope
            or pending.frame["request_id"] != called_scope.parent_request_id
            or pending.frame["invocation_ref"] != invocation_ref or pending.frame["method"] != method
            or pending.frame["deadline_at"] != called_scope.deadline_at
            or called_scope.deadline_at > scope.deadline_at
            or called_scope.deadline_at <= int(datetime.now(timezone.utc).timestamp() * 1000)):
        raise NativeServiceBlocked("native_memory_actual_source_call_changed")
    budget = source.admission.header_budget
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    return budget


def _native_memory_dispatch_witness_upper(witness):
    from src.memory.header_bounds import _native_memory_ascii_string_width
    # Already-original MappingProxy values; no dict/model/serialization to size.
    return 2 + max(0, len(witness) - 1) + sum(
        _native_memory_ascii_string_width(name) + 1 + _native_memory_source_value_upper(value)
        for name, value in witness.items())


def _native_memory_dispatch_witness_entries(journal_bytes, witness_upper):
    parsed = _native_memory_source_capacity(6 * journal_bytes + 2)
    witness_upper = _native_memory_source_capacity(witness_upper)
    return tuple((stage, upper) for stage, upper in (
        ("witness-json-parser", parsed), ("witness-selected-list", parsed),
        ("witness-payload-copy", witness_upper), ("witness-methods-list", witness_upper),
        ("witness-digest-canonical", witness_upper), ("witness-digest-utf8", witness_upper),
        ("witness-digest-return", 66), ("witness-method-set", witness_upper),
        ("witness-method-sort", witness_upper), ("witness-return-copy", witness_upper),
        ("witness-methods-tuple", witness_upper)))


def _native_memory_dispatch_binding_json_entries(text_bytes):
    from src.workflows.inference_accounting import _original_native_binding_numeric_entries
    # to_json: payload and digest.payload each construct one original manifest.
    # from_json/parser/constructor-only appearances are deliberately excluded.
    names = {"payload-methods-list", "payload-dependencies-list", "binding-payload", "dependency-payloads"}
    original = _original_native_binding_numeric_entries(None, text_bytes)
    entries = tuple((appearance[2], upper) for appearance, upper in original
        if type(appearance[2]) is tuple and appearance[2][0] == "manifest")
    for occurrence in range(2):
        entries += tuple((("payload", occurrence, appearance[2]), upper)
            for appearance, upper in original if appearance[2] in names)
    upper = _native_memory_source_capacity(6 * text_bytes + 2)
    return entries + (("digest-canonical", upper), ("digest-utf8", upper),
        ("digest-return", 66), ("final-dict-merge", upper), ("final-canonical", upper))


async def _validate_memory_dispatch_source(db, run, scope):
    """Current SQL writer checks remain required after private issuance."""
    from src.auth.ownership import _current_root
    from .dispatch import OriginalServiceInvocation, _witness
    from .ownership import validate_invocation
    if type(scope) is not OriginalServiceInvocation:
        raise NativeServiceBlocked("native_memory_actual_source_required")
    source = scope.native_memory_source
    if (type(source) is not _NativeMemoryDispatchSource or source._seal is not _MEMORY_SOURCE_SEAL
            or source._issued_id != id(source) or _MEMORY_SOURCES.get(id(source)) is not source
            or not source._live.is_set() or source._scope is not scope
            or source.report_source is not scope.native_memory_report_source):
        raise NativeServiceBlocked("native_memory_actual_source_unavailable")
    host, claim = source.host, source.claim
    if (not host.admitting or host.reviewed is None or claim._host is not host
            or host.boot_nonce != scope.host_boot_nonce or claim.host_boot_nonce != host.boot_nonce
            or host.reviewed.package_digest != scope.witness["package_digest"]
            or host.reviewed.composition_digest != scope.witness["host_composition_digest"]):
        raise NativeServiceBlocked("native_memory_actual_source_host_changed")
    from src.memory.header_bounds import _native_memory_utf8_width
    journal_bytes = _native_memory_utf8_width(run.checkpoint_receipts_json or "[]")
    witness_upper = _native_memory_dispatch_witness_upper(scope.witness)
    for appearance, upper in _native_memory_dispatch_witness_entries(journal_bytes, witness_upper):
        _native_memory_source_pay("dispatch-validation", appearance, upper)
    current = _witness(run, scope)
    candidate = source.admission.candidate()
    context = memory_context(run)
    _native_memory_source_pay("dispatch-validation", "scope-witness-copy", witness_upper)
    for stage in ("deadline-replace", "deadline-astimezone", "current-now"):
        _native_memory_source_pay("dispatch-validation", stage, 44)
    for appearance, upper in _native_memory_dispatch_binding_json_entries(
            _native_memory_utf8_width(run.composition_binding_json)):
        _native_memory_source_pay("dispatch-binding-json", appearance, upper)
    if (current != dict(scope.witness) or context["candidate"] != candidate
            or run.run_identity != claim.job["job_id"] or run.status != "running"
            or run.lease_owner != current["lease_owner"] or run.fencing_token != current["fencing_token"]
            or run.attempt_count != current["attempt_count"] or run.input_digest != source.admission.candidate_digest
            or _utc(run.deadline_at) <= datetime.now(timezone.utc)
            or run.composition_binding_json != claim.binding.to_json()):
        raise NativeServiceBlocked("native_memory_actual_source_claim_changed")
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, OPERATOR_SESSION
    budget = source.admission.header_budget
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    try:
        await budget.certify(db, OPERATOR_SESSION, (source.operator.session_id,), _header_budget=budget)
    except HeaderBoundsError as error:
        raise NativeServiceBlocked("native_memory_source_bound_not_certified") from error
    await _current_root(db, source.operator)
    await validate_invocation(db, claim.binding, header_budget=budget)
    return source


@dataclass(frozen=True, eq=False)
class _MemoryOwnerSourceOperation:
    """One genuine owner transaction, with an actual returned effect identity."""
    source: _NativeMemoryDispatchSource = field(repr=False)
    scope: object = field(repr=False)
    db: object = field(repr=False)
    transaction: object = field(repr=False)
    driver: object = field(repr=False)
    run: object = field(repr=False)
    _seal: object = field(default=None, init=False, repr=False)
    _issued_id: int = field(default=0, init=False, repr=False)
    _effect: object = field(default=None, init=False, repr=False)
    _prepared: object = field(default=None, init=False, repr=False)


async def _begin_memory_owner_source(db, run, scope, *, prepared=None):
    from src.memory.header_bounds import _connection_state
    source = await _validate_memory_dispatch_source(db, run, scope)
    transaction, driver, _changes = await _connection_state(db)
    operation = _MemoryOwnerSourceOperation(source, scope, db, transaction, driver, run)
    if prepared is not None:
        from src.memory.m5 import validate_native_memory_mutation_plan
        await validate_native_memory_mutation_plan(prepared, db=db,
            candidate_digest=source.admission.candidate_digest, native_memory_source=source)
        object.__setattr__(operation, "_prepared", prepared)
    object.__setattr__(operation, "_seal", _MEMORY_SOURCE_SEAL)
    object.__setattr__(operation, "_issued_id", id(operation))
    _MEMORY_OPERATIONS[id(operation)] = operation
    return operation


async def _validate_memory_owner_source(db, run, operation, *, effect=None):
    from src.memory.header_bounds import _connection_state
    if (type(operation) is not _MemoryOwnerSourceOperation or operation._seal is not _MEMORY_SOURCE_SEAL
            or operation._issued_id != id(operation) or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or operation.db is not db or operation.run is not run):
        raise NativeServiceBlocked("native_memory_actual_operation_unavailable")
    transaction, driver, _changes = await _connection_state(db)
    if transaction is not operation.transaction or driver is not operation.driver:
        raise NativeServiceBlocked("native_memory_actual_operation_stale")
    source = await _validate_memory_dispatch_source(db, run, operation.scope)
    if source is not operation.source or (effect is not None and operation._effect is not effect):
        raise NativeServiceBlocked("native_memory_actual_effect_unavailable")
    return source


def _memory_live_claim_source(original_claim):
    """Issuer identity only, before body reads; numeric routing, never consent."""
    from src.workflows.job_runtime import NativeServiceClaim
    if type(original_claim) is not NativeServiceClaim:
        raise NativeServiceBlocked("native_memory_actual_claim_source_required")
    sources = tuple(source for source in _MEMORY_SOURCES.values()
                    if source.claim is original_claim and source._live.is_set())
    if len(sources) != 1:
        raise NativeServiceBlocked("native_memory_actual_claim_source_unavailable")
    source = sources[0]
    if (source._seal is not _MEMORY_SOURCE_SEAL or source._issued_id != id(source)
            or source._scope.native_memory_source is not source
            or source.host is not original_claim._host or not source.host.admitting
            or source.host.boot_nonce != original_claim.host_boot_nonce):
        raise NativeServiceBlocked("native_memory_actual_claim_source_unavailable")
    return source


def _validate_memory_completion_capacity_source(source, original_claim):
    """Live issuer/task identity for prepaid capacity; never body permission."""
    import asyncio
    from src.auth.service import AuthenticatedOperator
    from src.memory.header_bounds import (
        HeaderReadBudget, _NativeMemoryNumericLedger, _MEMORY_LEDGER_SEAL)
    from src.workspace.accounting_witness import _NativeMemoryRetention
    from src.workflows.job_runtime import NativeServiceClaim
    from .bridge import CordisHost, cordis_host
    from .dispatch import OriginalServiceInvocation
    if (type(original_claim) is not NativeServiceClaim
            or type(source) is not _NativeMemoryDispatchSource
            or source.claim is not original_claim
            or source._seal is not _MEMORY_SOURCE_SEAL or source._issued_id != id(source)
            or _MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or type(source.admission) is not NativeMemoryMutationAdmission
            or type(source.operator) is not AuthenticatedOperator
            or type(source.host) is not CordisHost or source.host is not cordis_host
            or source.host is not original_claim._host or not source.host.admitting
            or source.host.reviewed is None
            or source.host.boot_nonce != original_claim.host_boot_nonce
            or type(source._scope) is not OriginalServiceInvocation
            or source._scope.native_memory_source is not source
            or source._scope.native_memory_report_source is not source.report_source
            or source._scope.binding is not original_claim.binding
            or source._scope.host_boot_nonce != original_claim.host_boot_nonce
            or source.host.reviewed.package_digest != source._scope.witness['package_digest']
            or source.host.reviewed.composition_digest != source._scope.witness['host_composition_digest']):
        raise NativeServiceBlocked("native_memory_completion_capacity_source_unavailable")
    retention = getattr(source, "_retention", None)
    if (type(retention) is not _NativeMemoryRetention or retention.source is not source
            or not retention.captured or retention.completed or retention.original is None
            or type(retention.ledger) is not _NativeMemoryNumericLedger
            or retention.ledger.source is not source
            or retention.ledger._complete_owner is not _MEMORY_LEDGER_SEAL
            or type(source.admission.header_budget) is not HeaderReadBudget
            or retention.ledger.budget is not source.admission.header_budget
            or retention.before['run_identity'] != original_claim.job['job_id']):
        raise NativeServiceBlocked("native_memory_completion_capacity_retention_unavailable")
    retention._checked()
    try:
        task = asyncio.current_task()
    except RuntimeError as error:
        raise NativeServiceBlocked("native_memory_completion_capacity_task_unavailable") from error
    if task is None or source._owner_task is not task:
        raise NativeServiceBlocked("native_memory_completion_capacity_task_unavailable")
    return source


def _validate_memory_completion_publication_capacity_source(source, original_claim, *, guard):
    """Only the applied proof pays the original writer exit; never body permission."""
    import asyncio
    from src.auth.service import AuthenticatedOperator
    from src.memory.header_bounds import (
        HeaderReadBudget, _NativeMemoryNumericLedger, _MEMORY_LEDGER_SEAL)
    from src.workspace.accounting_witness import (
        _NativeMemoryRetention, _NativeMemorySourcePublication, CompositionSessionGuard,
        _MEMORY_PUBLICATIONS, _COMPOSITION_METADATA_GUARDS)
    from src.workflows.job_runtime import NativeServiceClaim
    from .bridge import CordisHost, cordis_host
    from .dispatch import OriginalServiceInvocation
    if (type(original_claim) is not NativeServiceClaim
            or type(source) is not _NativeMemoryDispatchSource
            or source.claim is not original_claim
            or source._seal is not _MEMORY_SOURCE_SEAL or source._issued_id != id(source)
            or _MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or type(source.admission) is not NativeMemoryMutationAdmission
            or type(source.operator) is not AuthenticatedOperator
            or type(source.host) is not CordisHost or source.host is not cordis_host
            or source.host is not original_claim._host or not source.host.admitting
            or source.host.reviewed is None
            or source.host.boot_nonce != original_claim.host_boot_nonce
            or type(source._scope) is not OriginalServiceInvocation
            or source._scope.native_memory_source is not source
            or source._scope.native_memory_report_source is not source.report_source
            or source._scope.binding is not original_claim.binding
            or source._scope.host_boot_nonce != original_claim.host_boot_nonce
            or source.host.reviewed.package_digest != source._scope.witness['package_digest']
            or source.host.reviewed.composition_digest != source._scope.witness['host_composition_digest']):
        raise NativeServiceBlocked("native_memory_completion_capacity_source_unavailable")
    retention = getattr(source, "_retention", None)
    if (type(retention) is not _NativeMemoryRetention or retention.source is not source
            or not retention.captured or not retention.completed or retention.original is None
            or type(retention.ledger) is not _NativeMemoryNumericLedger
            or retention.ledger.source is not source
            or retention.ledger._complete_owner is not _MEMORY_LEDGER_SEAL
            or type(source.admission.header_budget) is not HeaderReadBudget
            or retention.ledger.budget is not source.admission.header_budget
            or retention.before['run_identity'] != original_claim.job['job_id']):
        raise NativeServiceBlocked("native_memory_completion_publication_retention_unavailable")
    retention._checked()
    try:
        task = asyncio.current_task()
    except RuntimeError as error:
        raise NativeServiceBlocked("native_memory_completion_publication_task_unavailable") from error
    plan = getattr(retention, "_completion_publication", None)
    binding = retention.ledger.bindings.get("completion")
    if (task is None or source._owner_task is not task
            or type(guard) is not CompositionSessionGuard
            or guard not in _COMPOSITION_METADATA_GUARDS or guard._retention_closed
            or guard.db.info.get("composition_guard") is not guard
            or getattr(guard, "_native_memory_source_retention", None) is not retention
            or guard.header_budget is not retention.ledger.budget
            or type(plan) is not _NativeMemorySourcePublication
            or _MEMORY_PUBLICATIONS.get(id(plan)) is not plan
            or plan.retention is not retention or plan.kind != "completion"
            or not plan.consumed or not plan.applied
            or getattr(plan, "_capacity_publication_live", False) is not True
            or getattr(plan, "_capacity_publication_spent", False)
            or plan.db is not guard.db or plan.task is not task
            or plan.run.run_identity != original_claim.job['job_id']
            or plan.transaction is not guard.db.sync_session.get_transaction()
            or not plan.driver.in_transaction
            or type(binding) is not tuple or len(binding) != 6
            or binding[0] is not guard.db or binding[1] is not plan.transaction
            or binding[2] is not plan.driver or binding[5] is not task):
        raise NativeServiceBlocked("native_memory_completion_publication_proof_unavailable")
    return source



def _memory_completion_capacity_source(original_claim, *, header_budget=None):
    """Resolve one genuine existing Source before writer startup allocations."""
    source = _memory_live_claim_source(original_claim)
    _validate_memory_completion_capacity_source(source, original_claim)
    if header_budget is not None and header_budget is not source.admission.header_budget:
        raise NativeServiceBlocked("native_memory_completion_capacity_frame_changed")
    return source


@asynccontextmanager
async def _memory_completion_capacity_scope(original_claim, *, header_budget=None):
    """Only the retained pre-effect ledger pays; current SQL checks stay separate."""
    source = _memory_completion_capacity_source(original_claim, header_budget=header_budget)
    async with source._retention.ledger.source_phase(source, "completion"):
        yield source


async def _validate_memory_lifecycle_source(db, run, original_claim):
    """Current original checks remain mandatory after issuer-only routing."""
    source = _memory_live_claim_source(original_claim)
    current = await _validate_memory_dispatch_source(db, run, source._scope)
    if source is not current:
        raise NativeServiceBlocked("native_memory_actual_claim_source_changed")
    return source


_MEMORY_REPORT_TRACE_ACTIVE = ContextVar("native_memory_report_dependency_trace", default=None)
_MEMORY_REPORT_TRACE_COLLECTORS = weakref.WeakValueDictionary()


def _native_memory_report_source_capacity_checked(source, db, budget):
    """Actual original writer Source supplies resource custody, never a grant."""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
    from src.extensions.capability_execution import _configured_mac_numeric_frame
    from src.workspace.accounting_witness import CompositionSessionGuard, _COMPOSITION_METADATA_GUARDS
    from src.workflows.job_runtime import NativeServiceClaim
    from .dispatch import OriginalServiceInvocation
    if (type(source) is not _NativeMemoryDispatchSource
            or _MEMORY_SOURCES.get(id(source)) is not source
            or source._seal is not _MEMORY_SOURCE_SEAL or source._issued_id != id(source)
            or not source._live.is_set() or type(db) is not AsyncSession
            or type(budget) is not HeaderReadBudget
            or type(source.admission) is not NativeMemoryMutationAdmission
            or source.admission.header_budget is not budget
            or type(source._scope) is not OriginalServiceInvocation
            or type(source.claim) is not NativeServiceClaim
            or source._scope.binding is not source.claim.binding
            or source._scope.witness["invocation_ref"] != source.claim.job["job_id"]
            or source._scope.witness["input_digest"] != source.admission.candidate_digest
            or source._scope.native_memory_source is not source
            or source.report_source is not source.admission.report_source
            or source._scope.native_memory_report_source is not source.report_source):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    witness = source.report_source
    if (type(witness) is not NativeMemoryReportSourceWitness
            or type(witness._identity) is not _MemoryReportSourceIssuance
            or witness._identity.source_identity != id(witness)
            or type(witness._input_stage) is not _OriginalMemoryReportInputStage
            or _MEMORY_REPORT_INPUT_STAGES.get(id(witness._input_stage)) is not witness._input_stage
            or witness._input_stage.budget is not budget):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    host, claim, scope = source.host, source.claim, source._scope
    if (not host.admitting or host.reviewed is None or claim._host is not host
            or host.boot_nonce != scope.host_boot_nonce
            or claim.host_boot_nonce != host.boot_nonce
            or host.reviewed.package_digest != scope.witness["package_digest"]
            or host.reviewed.composition_digest != scope.witness["host_composition_digest"]):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    guard = db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_authority_read_numeric", None)
    if (type(guard) is not CompositionSessionGuard or guard not in _COMPOSITION_METADATA_GUARDS
            or guard.db is not db or guard.header_budget is not budget
            or guard._retention_closed or not db.info.get("native_writer_started")
            or type(retained) is not tuple or len(retained) != 10
            or retained[0] is not source or retained[1] is not scope or retained[2] is not db
            or retained[3] is not db.sync_session.get_transaction()
            or retained[5] is not asyncio.current_task() or retained[6] is not budget
            or not retained[4].in_transaction):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    frame = _configured_mac_numeric_frame()
    if frame is not None and frame is not budget:
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    # Compare the original captured writer fields directly. Calling the
    # original snapshot builder here would construct a fresh nine-field tuple
    # for every trace debit; this new resource gate needs no such copy.
    import threading
    captured = guard._retention_writer_snapshot
    if (type(captured) is not tuple or len(captured) != 9
            or captured[0] is not db or captured[1] is not retained[5]
            or captured[2] != threading.get_ident()
            or captured[3] not in ("native_ingress", "composition_maintenance", "durable_jobs", "finite_service")
            or db.info.get("composition_writer_owner") != captured[3]
            or captured[4] is not budget or captured[6] is not retained[3]
            or captured[8] is not retained[4] or not retained[3].is_active
            or db.in_nested_transaction()):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    connection = captured[5]
    connection_record = retained[3]._connections.get(connection)
    if (connection_record is None or connection_record[0] is not connection
            or retained[3]._connections.get(db.sync_session.get_bind()) is not connection_record
            or connection_record[1] is not captured[7]
            or connection.closed or connection.invalidated
            or connection.get_transaction() is not captured[7] or not captured[7].is_active
            or connection.in_nested_transaction()
            or connection.connection.driver_connection is not retained[4]):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    return retained


def _native_memory_report_source_resource_owner(db_or_connection, budget):
    """Header owner accessor for only these authentic lexical Source modes."""
    collector = _MEMORY_REPORT_TRACE_ACTIVE.get()
    if collector is None or (type(collector) is _NativeMemoryReportTraceCollector
            and getattr(collector, "_source_mode", None) is None):
        return None
    from src.memory.header_bounds import HeaderBoundsError
    if (type(collector) is not _NativeMemoryReportTraceCollector
            or _MEMORY_REPORT_TRACE_COLLECTORS.get(id(collector)) is not collector):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    collector._checked()
    if collector.budget is not budget:
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    if db_or_connection is collector.db:
        return collector
    from sqlalchemy.engine import Connection
    transaction = collector.transaction
    if type(db_or_connection) is not Connection:
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    connection_record = transaction._connections.get(db_or_connection)
    if (connection_record is None or connection_record[0] is not db_or_connection
            or connection_record[1] is not db_or_connection.get_transaction()
            or not db_or_connection.in_transaction()
            or db_or_connection.connection.driver_connection is not collector.driver):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    return collector


def _begin_memory_report_source_numeric_trace(db, source, budget, mode):
    from src.memory.header_bounds import HeaderBoundsError, _MEMORY_CHARGE_TRACE
    from src.memory.composition_headers import _common_trace_label_width
    retained = _native_memory_report_source_capacity_checked(source, db, budget)
    if mode not in ("owner-session", "source-binding", "vault-consume") or _MEMORY_REPORT_TRACE_ACTIVE.get() is not None:
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    constructor_upper = 2 + 10 + 9 * 4 + 2 + _common_trace_label_width(mode)
    for name in ("db", "budget", "issuer_task", "transaction", "driver", "guard", "prior", "entries", "source", "_source_mode", "stage"):
        constructor_upper += _common_trace_label_width(name) + 1
    budget.debit(constructor_upper, appearance=("native-memory-source-trace", "collector-construction"))
    budget.debit(4, appearance=("native-memory-source-trace", "collector-weak-reference"))
    budget.debit(27, appearance=("native-memory-source-trace", "collector-registry-entry"))
    collector = object.__new__(_NativeMemoryReportTraceCollector)
    collector.db, collector.budget = db, budget
    collector.issuer_task, collector.transaction, collector.driver = retained[5], retained[3], retained[4]
    collector.guard, collector.prior = db.info["composition_guard"], _MEMORY_CHARGE_TRACE.get()
    collector.entries = []
    collector.source, collector._source_mode = source, mode
    collector.stage = source.report_source._input_stage
    _MEMORY_REPORT_TRACE_COLLECTORS[id(collector)] = collector
    budget.debit(4, appearance=("native-memory-source-trace", "active-context-token"))
    active_token = _MEMORY_REPORT_TRACE_ACTIVE.set(collector)
    trace_token = None
    try:
        collector._checked()
        budget.debit(4, appearance=("native-memory-source-trace", "trace-context-token"))
        budget.debit(11, appearance=("native-memory-source-trace", "trace-binding-tuple"))
        budget.debit(16, appearance=("native-memory-source-trace", "collector-return-tuple"))
        trace_token = _MEMORY_CHARGE_TRACE.set((budget, collector))
        return collector, active_token, trace_token
    except BaseException:
        if trace_token is not None:
            _MEMORY_CHARGE_TRACE.reset(trace_token)
        _MEMORY_REPORT_TRACE_ACTIVE.reset(active_token)
        _MEMORY_REPORT_TRACE_COLLECTORS.pop(id(collector), None)
        raise


def _finish_memory_report_source_numeric_trace(collector):
    from src.memory.composition_headers import _common_trace_label_width
    collector._checked()
    upper = 2 + max(0, len(collector.entries) - 1)
    for appearance, amount in collector.entries:
        upper += 3 + _common_trace_label_width(appearance) + 20
    collector._storage_debit(upper, "source-retained-core-tuple")
    entries = tuple(collector.entries)
    # Existing objects are borrowed; the actual immutable trace remains nested.
    collector._storage_debit(2 + 8 + 7 * 4 + _common_trace_label_width(collector._source_mode) + upper,
        "source-retained-lineage")
    return (collector.source, collector.stage, collector.db, collector.transaction,
        collector.driver, collector.issuer_task, collector.budget, collector._source_mode, entries)


def _checked_memory_report_source_trace_certificate(collector, certificate, descriptor, identity):
    from src.memory.header_bounds import HeaderBoundsError, HeaderCertificate, _SEAL
    collector._checked()
    if (type(certificate) is not HeaderCertificate or getattr(certificate, "_seal", None) is not _SEAL
            or getattr(certificate, "_issued_id", None) != id(certificate)
            or certificate.descriptor is not descriptor or certificate._db is not collector.db
            or certificate._transaction is not collector.transaction or certificate._driver is not collector.driver
            or certificate._changes != collector.driver.total_changes
            or len(certificate.row_ids) != 1 or certificate.row_ids[0] != identity):
        raise HeaderBoundsError("memory_report_source_resource_certificate_changed")


def _memory_report_source_for_revalidation(db, admission):
    from src.memory.header_bounds import HeaderBoundsError
    guard = db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_authority_read_numeric", None)
    if retained is None:
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
        if db.info.get("native_writer_started") or _MEMORY_LEDGER_PHASE.get() is not None:
            raise HeaderBoundsError("memory_report_source_resource_owner_changed")
        return None
    if type(retained) is not tuple or len(retained) != 10:
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    source = retained[0]
    _native_memory_report_source_capacity_checked(source, db, admission.header_budget)
    if source.admission is not admission:
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    return source


def _commit_memory_report_source_numeric_trace(source, proof, session_trace=None, *, recheck=False, read_current=False):
    if source is None:
        return
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    pending = getattr(proof, "_source_binding_numeric_pending", None)
    if type(pending) is not tuple or len(pending) != 9 or pending[0] is not source:
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    retained = _native_memory_report_source_capacity_checked(source, pending[2], pending[6])
    stage, budget = source.report_source._input_stage, pending[6]
    if (pending[1] is not stage or pending[3] is not retained[3]
            or pending[4] is not retained[4] or pending[5] is not retained[5]
            or pending[7] != "source-binding"):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    if session_trace is not None and (type(session_trace) is not tuple or len(session_trace) != 9
            or session_trace[0] is not source or session_trace[1] is not stage
            or session_trace[2] is not pending[2] or session_trace[3] is not pending[3]
            or session_trace[4] is not pending[4] or session_trace[5] is not pending[5]
            or session_trace[6] is not budget or session_trace[7] != "owner-session"):
        raise HeaderBoundsError("memory_report_source_resource_owner_changed")
    # Borrowed immutable trace objects; no copy of body or metadata values.
    _memory_report_stage_pay(budget, "source-binding-success-lineage", 2 + 3 + 4 + 4 + 5 + 5)
    confirmed = (pending, session_trace, recheck, read_current)
    name = "_source_binding_recheck_numeric_confirmed" if recheck else "_source_binding_owner_numeric_confirmed"
    _memory_report_stage_pay(budget, "source-binding-success-slot", 3 + _native_memory_ascii_string_width(name) + 4)
    object.__setattr__(stage, name, confirmed)
    object.__delattr__(proof, "_source_binding_numeric_pending")


class _NativeMemoryReportTraceCollector:
    """Original REPORT8 issuer's resource observations, never read permission."""
    def __init__(self, db, budget, task, certificates, prior):
        import asyncio
        self.db, self.budget, self.task_row = db, budget, task
        self.issuer_task = asyncio.current_task()
        self.transaction = db.sync_session.get_transaction()
        self.driver = certificates[0]._driver
        self.certificates = certificates
        self.guard = db.info.get("composition_guard")
        self.prior = prior
        self.entries = []

    def _checked(self):
        from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, HeaderCertificate, _SEAL
        if (_MEMORY_REPORT_TRACE_COLLECTORS.get(id(self)) is not self
                or _MEMORY_REPORT_TRACE_ACTIVE.get() is not self):
            raise HeaderBoundsError("memory_report_numeric_trace_changed")
        if getattr(self, "_source_mode", None) is not None:
            retained = _native_memory_report_source_capacity_checked(self.source, self.db, self.budget)
            if (self._source_mode not in ("owner-session", "source-binding", "vault-consume")
                    or self.issuer_task is not retained[5] or self.transaction is not retained[3]
                    or self.driver is not retained[4] or self.guard is not self.db.info.get("composition_guard")
                    or self.stage is not self.source.report_source._input_stage):
                raise HeaderBoundsError("memory_report_source_resource_owner_changed")
            return
        import asyncio
        from sqlalchemy.ext.asyncio import AsyncSession
        from src.extensions.capability_execution import _configured_mac_numeric_frame
        from src.workspace.accounting_witness import CompositionSessionGuard, _COMPOSITION_METADATA_GUARDS
        from src.db.models import WorkBoardTask
        from src.memory.header_bounds import WORK_BOARD_TASK, WORK_BOARD_ATTEMPT, GOAL, WRS_BY_RUN
        frame = _configured_mac_numeric_frame()
        if (type(self.db) is not AsyncSession or type(self.budget) is not HeaderReadBudget
                or (frame is not None and frame is not self.budget)
                or self.issuer_task is None or self.issuer_task is not asyncio.current_task()
                or self.transaction is None or self.db.sync_session.get_transaction() is not self.transaction
                or not self.driver.in_transaction or type(self.task_row) is not WorkBoardTask
                or self.task_row.capability_id != REPORT_SOURCE_CAPABILITY
                or type(self.certificates) is not list or len(self.certificates) != 4):
            raise HeaderBoundsError("memory_report_numeric_trace_changed")
        for index in range(4):
            descriptor = (WORK_BOARD_TASK if index == 0 else WORK_BOARD_ATTEMPT if index == 1
                else GOAL if index == 2 else WRS_BY_RUN)
            certificate = self.certificates[index]
            if (type(certificate) is not HeaderCertificate or certificate._seal is not _SEAL
                    or certificate._issued_id != id(certificate) or certificate.descriptor is not descriptor
                    or certificate._db is not self.db or certificate._transaction is not self.transaction
                    or certificate._driver is not self.driver or certificate._changes != self.driver.total_changes):
                raise HeaderBoundsError("memory_report_numeric_trace_changed")
        if (len(self.certificates[0].row_ids) != 1 or self.certificates[0].row_ids[0] != self.task_row.task_id
                or len(self.certificates[2].row_ids) != 1 or self.certificates[2].row_ids[0] != self.task_row.goal_id):
            raise HeaderBoundsError("memory_report_numeric_trace_changed")
        guard = self.db.info.get("composition_guard")
        if guard is not self.guard or (guard is not None and (
                type(guard) is not CompositionSessionGuard or guard not in _COMPOSITION_METADATA_GUARDS
                or guard.db is not self.db or guard.header_budget is not self.budget or guard._retention_closed)):
            raise HeaderBoundsError("memory_report_numeric_trace_changed")
        if self.db.info.get("native_writer_started"):
            if guard is None:
                raise HeaderBoundsError("memory_report_numeric_trace_changed")
            guard._validate_native_writer_snapshot(self.budget)
        if getattr(self, "_metadata_record", None) is not None:
            _checked_memory_report_metadata_numeric_owner(self.db, self.task_row, self.budget,
                self._metadata_record, self._metadata_input_stage)
            if (len(self.certificates[1].row_ids) != 1
                    or self.certificates[1].row_ids[0] != self._metadata_input_stage.attempt_id):
                raise HeaderBoundsError("memory_report_metadata_trace_changed")

    def _storage_debit(self, upper, stage):
        from src.memory.header_bounds import _MEMORY_CHARGE_TRACE
        token = _MEMORY_CHARGE_TRACE.set(self.prior)
        try:
            self.budget.debit(upper, appearance=("native-memory-report-trace", stage, len(self.entries)))
        finally:
            _MEMORY_CHARGE_TRACE.reset(token)

    def append_charge(self, appearance, amount):
        from src.memory.header_bounds import HeaderBoundsError
        from src.memory.composition_headers import _common_trace_label_width
        self._checked()
        if type(amount) is not int or not 0 <= amount < 2**63:
            raise HeaderBoundsError("memory_report_numeric_trace_changed")
        pair_upper = 3 + _common_trace_label_width(appearance) + 20
        self._storage_debit(pair_upper, "entry-pair")
        self._storage_debit(pair_upper + 1, "entry-list-slot")
        entry = (appearance, amount)
        self.entries.append(entry)
        if self.prior is not None and self.prior[0] is self.budget:
            from src.memory.composition_headers import _CommonCertificationTraceCollector
            from src.workflows.job_runtime import _NativeMemoryClaimTraceCollector
            from src.workspace.accounting_witness import _NativeMemoryAccountingTraceCollector
            outer = self.prior[1]
            if (type(outer) is _CommonCertificationTraceCollector or type(outer) is _MemoryAuthorityTraceCollector
                    or type(outer) is _NativeMemoryClaimTraceCollector or type(outer) is _NativeMemoryAccountingTraceCollector
                    or type(outer) is _NativeMemoryReportTraceCollector):
                outer.append_charge(appearance, amount)
            elif type(outer) is list:
                outer.append(entry)
            else:
                raise HeaderBoundsError("memory_report_numeric_trace_changed")


def _begin_memory_report_numeric_trace(db, task, budget, certificates, *,
        _metadata_record=None, _metadata_input_stage=None):
    from src.memory.header_bounds import HeaderBoundsError, _MEMORY_CHARGE_TRACE
    from src.memory.composition_headers import _common_trace_label_width
    # Borrow only the original SourceBinding's four already-issued certs.
    if type(certificates) is not list or len(certificates) != 4 or _MEMORY_REPORT_TRACE_ACTIVE.get() is not None:
        raise HeaderBoundsError("memory_report_numeric_trace_changed")
    constructor_upper = 2 + 9 + 9 * 4 + 2
    for name in ("db", "budget", "task_row", "issuer_task", "transaction", "driver", "certificates", "guard", "prior", "entries"):
        constructor_upper += _common_trace_label_width(name) + 1
    budget.debit(constructor_upper, appearance=("native-memory-report-trace", "collector-construction"))
    budget.debit(4, appearance=("native-memory-report-trace", "collector-weak-reference"))
    budget.debit(27, appearance=("native-memory-report-trace", "collector-registry-entry"))
    if _metadata_record is not None:
        _checked_memory_report_metadata_numeric_owner(db, task, budget, _metadata_record, _metadata_input_stage)
        for name, stage in (("_metadata_record", "metadata-record-slot"),
                ("_metadata_input_stage", "metadata-input-stage-slot")):
            budget.debit(3 + _common_trace_label_width(name) + 4,
                appearance=("native-memory-report-trace", stage))
        budget.debit(21, appearance=("native-memory-report-trace", "metadata-certificates-tuple"))
    elif _metadata_input_stage is not None:
        raise HeaderBoundsError("memory_report_metadata_trace_changed")
    collector = _NativeMemoryReportTraceCollector(db, budget, task, certificates, _MEMORY_CHARGE_TRACE.get())
    if _metadata_record is not None:
        collector._metadata_record = _metadata_record
        collector._metadata_input_stage = _metadata_input_stage
    _MEMORY_REPORT_TRACE_COLLECTORS[id(collector)] = collector
    budget.debit(4, appearance=("native-memory-report-trace", "active-context-token"))
    active_token = _MEMORY_REPORT_TRACE_ACTIVE.set(collector)
    trace_token = None
    try:
        collector._checked()
        budget.debit(4, appearance=("native-memory-report-trace", "trace-context-token"))
        budget.debit(11, appearance=("native-memory-report-trace", "trace-binding-tuple"))
        budget.debit(16, appearance=("native-memory-report-trace", "collector-return-tuple"))
        trace_token = _MEMORY_CHARGE_TRACE.set((budget, collector))
        return collector, active_token, trace_token
    except BaseException:
        if trace_token is not None:
            _MEMORY_CHARGE_TRACE.reset(trace_token)
        _MEMORY_REPORT_TRACE_ACTIVE.reset(active_token)
        _MEMORY_REPORT_TRACE_COLLECTORS.pop(id(collector), None)
        raise


def _checked_memory_report_metadata_numeric_owner(db, task, budget, record, stage):
    """Existing issued REPORT8 custody; numeric observation grants no read."""
    from src.memory.header_bounds import HeaderBoundsError
    from src.work_board.review import (_memory_report_row_certificate,
        _MemoryReportDependencyNumericInventory, _MEMORY_REPORT_DEPENDENCY_NUMERIC,
        _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL)
    if (type(record) is not _MemoryReportDependencyNumericInventory
            or _MEMORY_REPORT_DEPENDENCY_NUMERIC.get(record) is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL
            or getattr(budget, "_native_memory_report_dependency_numeric", None) is not record
            or record.db is not db or record.budget is not budget
            or type(stage) is not _OriginalMemoryReportInputStage
            or _MEMORY_REPORT_INPUT_STAGES.get(id(stage)) is not stage
            or stage.budget is not budget or stage.task_id != task.task_id
            or type(stage.input_bytes) is not bytes or type(stage.workspace_bytes) is not bytes
            or type(stage._identity) is not _MemoryReportSourceIssuance
            or stage._identity.source_identity != id(stage)):
        raise HeaderBoundsError("memory_report_metadata_trace_changed")
    for name in ("operator_sessions", "goals", "work_board_tasks", "work_board_attempts",
            "workflow_run_states", "work_board_input_artifacts", "work_board_proposals", "work_board_events"):
        _memory_report_row_certificate(db, task, budget, name)


def _finish_memory_report_metadata_numeric_trace(collector, certificates):
    collector._checked()
    from src.memory.composition_headers import _common_trace_label_width
    core_upper = 2 + max(0, len(collector.entries) - 1)
    for appearance, amount in collector.entries:
        core_upper += 3 + _common_trace_label_width(appearance) + 20
    collector._storage_debit(core_upper, "metadata-retained-core-tuple")
    entries = tuple(collector.entries)
    lineage_upper = 2 + 8 + 7 * 4 + 21 + core_upper
    collector._storage_debit(lineage_upper, "metadata-retained-lineage")
    collector._storage_debit(3 + _common_trace_label_width("_report_metadata_numeric_trace") + 4,
        "metadata-record-slot")
    lineage = (collector._metadata_record, collector._metadata_input_stage, collector.db,
        collector.transaction, collector.driver, collector.issuer_task, collector.budget,
        certificates, entries)
    object.__setattr__(collector._metadata_record, "_report_metadata_numeric_trace", lineage)


def _native_memory_report_metadata_numeric_lineage(operation, prepared, *,
        certificate, facts, retention_widths):
    from src.memory.header_bounds import HeaderBoundsError
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    stage = operation.source.report_source._input_stage
    _checked_memory_report_metadata_numeric_owner(operation.db, record, record.budget, record, stage)
    retained = getattr(record, "_report_metadata_numeric_trace", None)
    if (type(retained) is not tuple or len(retained) != 9 or retained[0] is not record
            or retained[1] is not stage or retained[2] is not operation.db
            or retained[3] is not operation.transaction or retained[4] is not operation.driver
            or retained[5] is not record.issuer_task or retained[6] is not record.budget
            or type(retained[7]) is not tuple or len(retained[7]) != 4
            or type(retained[8]) is not tuple):
        raise HeaderBoundsError("memory_report_metadata_trace_unavailable")
    from src.memory.header_bounds import HeaderCertificate, WORK_BOARD_TASK, WORK_BOARD_ATTEMPT, GOAL, WRS_BY_RUN, _SEAL
    for index in range(4):
        cert = retained[7][index]
        expected = WORK_BOARD_TASK if index == 0 else WORK_BOARD_ATTEMPT if index == 1 else GOAL if index == 2 else WRS_BY_RUN
        if (type(cert) is not HeaderCertificate or cert._seal is not _SEAL or cert._issued_id != id(cert)
                or cert.descriptor is not expected or cert._db is not operation.db
                or cert._transaction is not operation.transaction or cert._driver is not operation.driver
                or cert._changes != operation.driver.total_changes):
            raise HeaderBoundsError("memory_report_metadata_trace_unavailable")
    return retained


def _end_memory_report_numeric_trace(collector, active_token, trace_token):
    from src.memory.header_bounds import _MEMORY_CHARGE_TRACE
    _MEMORY_CHARGE_TRACE.reset(trace_token)
    _MEMORY_REPORT_TRACE_ACTIVE.reset(active_token)
    _MEMORY_REPORT_TRACE_COLLECTORS.pop(id(collector), None)


def _native_memory_report_dependency_numeric_inventory(operation, prepared, *, certificate, facts, retention_widths):
    """Exact original REPORT certificates for sizing; no private read grant."""
    import asyncio
    from src.workspace.accounting_witness import CompositionSessionGuard
    from src.memory.m5 import PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS
    from src.memory.header_bounds import HeaderBoundsError, HeaderCertificate, COMPOSITION_DESCRIPTORS, _SEAL
    from src.work_board.review import (_MemoryReportDependencyNumericInventory,
        _MEMORY_REPORT_DEPENDENCY_NUMERIC, _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL)
    if (type(operation) is not _MemoryOwnerSourceOperation
            or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or operation._seal is not _MEMORY_SOURCE_SEAL or operation._issued_id != id(operation)
            or _MEMORY_SOURCES.get(id(operation.source)) is not operation.source
            or operation.source._seal is not _MEMORY_SOURCE_SEAL
            or operation.source._issued_id != id(operation.source) or not operation.source._live.is_set()
            or type(prepared) is not PreparedNativeMemoryMutation
            or _NATIVE_MUTATION_PLANS.get(id(prepared)) is not prepared
            or operation._prepared is not prepared or prepared.consumed or operation._effect is not None
            or operation.transaction is not operation.db.sync_session.get_transaction()
            or not operation.driver.in_transaction):
        raise HeaderBoundsError("memory_report_dependency_numeric_owner_changed")
    guard = operation.db.info.get("composition_guard")
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    retained = getattr(guard, "_native_memory_source_capacity_facts", None)
    widths = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if (type(guard) is not CompositionSessionGuard or guard.db is not operation.db
            or owner is None or len(owner) != 6 or owner[0] is not operation
            or owner[1] is not prepared or owner[2] is not certificate
            or owner[3] is not asyncio.current_task() or owner[4] is not operation.transaction
            or owner[5] is not operation.driver
            or retained is None or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not facts
            or widths is None or widths[0] is not operation or widths[1] is not prepared
            or widths[2] is not certificate or widths[3] is not retention_widths):
        raise HeaderBoundsError("memory_report_dependency_numeric_owner_changed")
    budget = guard.header_budget
    record = getattr(budget, "_native_memory_report_dependency_numeric", None)
    witness = operation.source.admission.report_source
    if (type(witness) is not NativeMemoryReportSourceWitness
            or type(witness._input_stage) is not _OriginalMemoryReportInputStage
            or _MEMORY_REPORT_INPUT_STAGES.get(id(witness._input_stage)) is not witness._input_stage
            or witness._input_stage.budget is not budget
            or type(record) is not _MemoryReportDependencyNumericInventory
            or record._seal is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL
            or record._issued_id != id(record)
            or _MEMORY_REPORT_DEPENDENCY_NUMERIC.get(record) is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL
            or record.db is not operation.db or record.budget is not budget
            or record.issuer_task is not asyncio.current_task()
            or record.transaction is not operation.transaction or record.driver is not operation.driver
            or record.task_id != witness._input_stage.task_id
            or record.owner_principal_id != operation.source.operator.principal.principal_id
            or record.owner_session_id != operation.source.operator.session_id
            or type(record.certificates) is not tuple or len(record.certificates) != 8
            or type(record.numeric_trace) is not tuple):
        raise HeaderBoundsError("memory_report_dependency_numeric_owner_changed")
    for index, name in enumerate(("operator_sessions", "goals", "work_board_tasks", "work_board_attempts",
            "workflow_run_states", "work_board_input_artifacts", "work_board_proposals", "work_board_events")):
        exact = record.certificates[index]
        if (type(exact) is not HeaderCertificate or exact._seal is not _SEAL
                or exact._issued_id != id(exact) or exact.descriptor is not COMPOSITION_DESCRIPTORS[name]
                or exact._db is not operation.db or exact._transaction is not operation.transaction
                or exact._driver is not operation.driver or exact._changes != prepared._total_changes):
            raise HeaderBoundsError("memory_report_dependency_numeric_certificate_changed")
    return record


def _native_memory_source_binding_numeric_lineage(operation, prepared, *,
        certificate, facts, retention_widths):
    """Borrow success-only original SESSION/Source observations, never a grant."""
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    source = operation.source
    stage = source.admission.report_source._input_stage
    confirmed = getattr(stage, "_source_binding_owner_numeric_confirmed", None)
    if (type(confirmed) is not tuple or len(confirmed) != 4
            or confirmed[2] is not False or confirmed[3] is not False):
        raise HeaderBoundsError("memory_source_authority_source_binding_certificate_trace_unavailable")
    for index in range(2):
        lineage = confirmed[index]
        mode = "source-binding" if index == 0 else "owner-session"
        if (type(lineage) is not tuple or len(lineage) != 9
                or lineage[0] is not source or lineage[1] is not stage
                or lineage[2] is not operation.db or lineage[3] is not operation.transaction
                or lineage[4] is not operation.driver or lineage[5] is not record.issuer_task
                or lineage[6] is not record.budget or lineage[7] != mode
                or type(lineage[8]) is not tuple or not lineage[8]):
            raise HeaderBoundsError("memory_source_authority_source_binding_certificate_trace_changed")
        for entry in lineage[8]:
            if (type(entry) is not tuple or len(entry) != 2
                    or type(entry[0]) is not tuple or type(entry[1]) is not int
                    or not 0 <= entry[1] <= MAX_BYTES):
                raise HeaderBoundsError("memory_source_authority_source_binding_certificate_trace_changed")
    return confirmed


def original_native_memory_source_binding_trace_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """Original SESSION then Source-binding observations and their storage.

    These are actual original lexical regions, not a complete owner graph.
    Original Source rows stay unchanged; no future certificate is fabricated.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width as width
    from src.workspace.accounting_witness import _native_memory_capacity_label_width
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_source_binding_trace_constructor_mode_changed")
    confirmed = _native_memory_source_binding_numeric_lineage(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    entries = None if _construction_only else []
    constructor = pair_values = events = 0
    for mode_index in range(2):
        lineage = confirmed[1] if mode_index == 0 else confirmed[0]
        mode = lineage[7]
        core = lineage[8]
        collector_upper = 2 + 10 + 9 * 4 + 2 + width(mode)
        for name in ("db", "budget", "issuer_task", "transaction", "driver", "guard", "prior", "entries", "source", "_source_mode", "stage"):
            collector_upper += width(name) + 1
        for stage in ("collector-construction", "collector-weak-reference", "collector-registry-entry",
                "active-context-token", "trace-context-token", "trace-binding-tuple", "collector-return-tuple"):
            upper = collector_upper if stage == "collector-construction" else (27 if stage == "collector-registry-entry"
                else 11 if stage == "trace-binding-tuple" else 16 if stage == "collector-return-tuple" else 4)
            label_upper = 3 + width("native-memory-source-trace") + width(stage)
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-source-trace", stage), upper))
        retained_upper = 2 + max(0, len(core) - 1)
        for index in range(len(core)):
            entry = core[index]
            label_upper = _native_memory_capacity_label_width(entry[0], operation=operation,
                prepared=prepared, certificate=certificate)
            original_pair_upper = 3 + label_upper + 20
            retained_upper += original_pair_upper
            # append_charge prepays these two original appearances before it
            # appends the original debit's pair to the observation list.
            for stage in ("entry-pair", "entry-list-slot"):
                upper = original_pair_upper + (1 if stage == "entry-list-slot" else 0)
                storage_label_upper = 4 + width("native-memory-report-trace") + width(stage) + 20
                pair_upper = 3 + storage_label_upper + 20
                constructor += storage_label_upper + pair_upper
                pair_values += pair_upper
                events += 1
                if not _construction_only:
                    entries.append((("native-memory-report-trace", stage, index), upper))
            # Borrow this exact immutable observed pair: it is not relabeled,
            # reconstructed or confused with another certificate issuer.
            pair_values += original_pair_upper
            events += 1
            if not _construction_only:
                entries.append(entry)
        for stage in ("source-retained-core-tuple", "source-retained-lineage"):
            upper = retained_upper if stage == "source-retained-core-tuple" else 2 + 8 + 7 * 4 + width(mode) + retained_upper
            if not 0 <= upper <= MAX_BYTES:
                raise HeaderBoundsError("memory_source_binding_trace_numeric_upper_unavailable")
            label_upper = 4 + width("native-memory-report-trace") + width(stage) + 20
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-report-trace", stage, len(core)), upper))
    constructor += 2 * (2 + max(0, events - 1) + pair_values)
    if _construction_only:
        return constructor
    return tuple(entries)


def _native_memory_source_vault_numeric_lineage(operation, prepared, *,
        certificate, facts, retention_widths):
    from src.memory.header_bounds import HeaderBoundsError
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    source = operation.source
    stage = source.report_source._input_stage
    current = _native_memory_report_source_capacity_checked(source, operation.db, record.budget)
    retained = getattr(stage, "_source_vault_numeric_confirmed", None)
    if (type(retained) is not tuple or len(retained) != 9 or retained[0] is not source
            or retained[1] is not stage or retained[2] is not operation.db
            or retained[3] is not current[3] or retained[4] is not current[4]
            or retained[5] is not current[5] or retained[6] is not record.budget
            or retained[7] != "vault-consume" or type(retained[8]) is not tuple):
        raise HeaderBoundsError("memory_source_vault_numeric_trace_unavailable")
    for entry in retained[8]:
        if (type(entry) is not tuple or len(entry) != 2 or type(entry[1]) is not int
                or not 0 <= entry[1] <= 2**63 - 1):
            raise HeaderBoundsError("memory_source_vault_numeric_trace_unavailable")
    return retained


def original_native_memory_source_vault_consume_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE original successful text/Vault consume with its trace storage."""
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width as width
    from src.workspace.accounting_witness import _native_memory_capacity_label_width
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_source_binding_trace_constructor_mode_changed")
    confirmed = _native_memory_source_vault_numeric_lineage(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    entries = None if _construction_only else []
    constructor = pair_values = events = 0
    for mode_index in range(1):
        lineage = confirmed
        mode = lineage[7]
        core = lineage[8]
        collector_upper = 2 + 10 + 9 * 4 + 2 + width(mode)
        for name in ("db", "budget", "issuer_task", "transaction", "driver", "guard", "prior", "entries", "source", "_source_mode", "stage"):
            collector_upper += width(name) + 1
        for stage in ("collector-construction", "collector-weak-reference", "collector-registry-entry",
                "active-context-token", "trace-context-token", "trace-binding-tuple", "collector-return-tuple"):
            upper = collector_upper if stage == "collector-construction" else (27 if stage == "collector-registry-entry"
                else 11 if stage == "trace-binding-tuple" else 16 if stage == "collector-return-tuple" else 4)
            label_upper = 3 + width("native-memory-source-trace") + width(stage)
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-source-trace", stage), upper))
        retained_upper = 2 + max(0, len(core) - 1)
        for index in range(len(core)):
            entry = core[index]
            label_upper = _native_memory_capacity_label_width(entry[0], operation=operation,
                prepared=prepared, certificate=certificate)
            original_pair_upper = 3 + label_upper + 20
            retained_upper += original_pair_upper
            # append_charge prepays these two original appearances before it
            # appends the original debit's pair to the observation list.
            for stage in ("entry-pair", "entry-list-slot"):
                upper = original_pair_upper + (1 if stage == "entry-list-slot" else 0)
                storage_label_upper = 4 + width("native-memory-report-trace") + width(stage) + 20
                pair_upper = 3 + storage_label_upper + 20
                constructor += storage_label_upper + pair_upper
                pair_values += pair_upper
                events += 1
                if not _construction_only:
                    entries.append((("native-memory-report-trace", stage, index), upper))
            # Borrow this exact immutable observed pair: it is not relabeled,
            # reconstructed or confused with another certificate issuer.
            pair_values += original_pair_upper
            events += 1
            if not _construction_only:
                entries.append(entry)
        for stage in ("source-retained-core-tuple", "source-retained-lineage", "source-vault-success-slot"):
            upper = (retained_upper if stage == "source-retained-core-tuple" else
                3 + width("_source_vault_numeric_confirmed") + 4 if stage == "source-vault-success-slot" else
                2 + 8 + 7 * 4 + width(mode) + retained_upper)
            if not 0 <= upper <= MAX_BYTES:
                raise HeaderBoundsError("memory_source_binding_trace_numeric_upper_unavailable")
            label_upper = 4 + width("native-memory-report-trace") + width(stage) + 20
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-report-trace", stage, len(core)), upper))
    constructor += 2 * (2 + max(0, events - 1) + pair_values)
    if _construction_only:
        return constructor
    return tuple(entries)


def original_native_memory_source_readback_digest_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE original proof digest from its own paid scalar observations.

    Scalar mode sizes this helper's new labels, pairs, list and returned tuple.
    The caller pays that constructor before default mode; neither mode grants
    a read or returns a complete Source authority reservation.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width as width
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_source_readback_constructor_mode_changed")
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    fields = getattr(record, "_source_readback_digest_numeric_fields", None)
    names = ("receipt_kind", "workflow_run_id", "status", "content_sha256", "readback_id",
        "artifact_id", "verification_id", "verifier_id", "effect_id_digest", "digest", "verified_at")
    if type(fields) is not tuple or len(fields) != len(names):
        raise HeaderBoundsError("memory_source_readback_numeric_fields_unavailable")
    for field in fields:
        if (type(field) is not tuple or len(field) != 4
                or type(field[0]) is not bool or type(field[1]) is not bool
                or type(field[2]) is not int or not 0 <= field[2] <= MAX_BYTES
                or type(field[3]) is not int or not 0 <= field[3] <= MAX_BYTES
                or (not field[0] and (field[1] or field[2] or field[3]))
                or (not field[1] and field[2] != field[3])):
            raise HeaderBoundsError("memory_source_readback_numeric_fields_changed")
    # Only default mode constructs output. The same literal stage roster is
    # counted in scalar mode without pairs, labels, a list or field copies.
    entries = None if _construction_only else []
    constructor = pair_values = events = raw_values = normalized_values = keys_upper = count = sorted_pairs = 0
    prefix_upper = width("native-memory-report-stage")
    for index in range(len(names)):
        field = fields[index]
        if not field[0]:
            continue
        key_upper = width(names[index])
        keys_upper += key_upper + 1
        raw_values += field[2]
        normalized_values += field[3]
        sorted_pairs += 3 + key_upper + field[2]
        count += 1
        for stage in ("readback-sort-key-str", "readback-key-str",
                "readback-key-nfc-scratch", "readback-key-nfc-output"):
            label_upper = 3 + prefix_upper + width(stage)
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-report-stage", stage), key_upper))
        if field[1]:
            for stage in ("readback-value-nfc-scratch", "readback-value-nfc-output"):
                label_upper = 3 + prefix_upper + width(stage)
                pair_upper = 3 + label_upper + 20
                constructor += label_upper + pair_upper
                pair_values += pair_upper
                events += 1
                if not _construction_only:
                    entries.append((("native-memory-report-stage", stage), field[3]))
    punctuation = 2 + max(0, count - 1)
    raw_map = punctuation + keys_upper + raw_values
    normalized_map = punctuation + keys_upper + normalized_values
    for stage in ("readback-safe-map", "readback-items-view", "readback-sorted-pairs",
            "readback-normalized-map", "readback-canonical", "readback-utf8", "readback-hexdigest"):
        if stage == "readback-safe-map":
            upper = raw_map
        elif stage == "readback-items-view":
            upper = 4
        elif stage == "readback-sorted-pairs":
            upper = punctuation + sorted_pairs
        elif stage == "readback-hexdigest":
            upper = 66
        else:
            upper = normalized_map
        if upper > MAX_BYTES:
            raise HeaderBoundsError("memory_source_readback_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    # Both numerical containers use the existing encoded pair grammar.
    constructor += 2 * (2 + max(0, events - 1) + pair_values)
    if _construction_only:
        return constructor
    return tuple(entries)


def _native_memory_source_authority_numeric_issuance(operation, prepared, entries, *,
        certificate, facts, retention_widths):
    """Exact issued numerical output custody; it supplies no read permission."""
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    issued = getattr(record, "_source_authority_numeric_complete", None)
    if (type(issued) is not tuple or len(issued) != 10
            or issued[0] is not operation or issued[1] is not prepared
            or issued[2] is not certificate or issued[3] is not facts
            or issued[4] is not retention_widths or issued[5] is not asyncio.current_task()
            or issued[6] is not operation.transaction or issued[7] is not operation.driver
            or issued[8] is not record.budget or issued[9] is not entries
            or type(entries) is not tuple or not entries):
        raise HeaderBoundsError("memory_source_authority_numeric_issuance_unavailable")
    return entries


async def original_native_memory_source_authority_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE original owner validator, only when its entire graph is closed.

    Authentic existing observations are checked before any output vector is
    constructed. A named missing original term denies the complete schedule;
    known isolated contributions are never returned as a partial owner total.
    """
    from src.workspace.accounting_witness import _native_memory_generic_future_bound_inputs
    from src.workflows.job_runtime import _native_memory_completion_bound_input_construction_upper
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_source_authority_constructor_mode_changed")
    # Scalar planning authenticates the existing owner without constructing
    # projection dictionaries/sets. The original strong default validator is
    # retained and is paid by the parent before its actual default invocation.
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    construction = _native_memory_completion_bound_input_construction_upper(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    construction += original_native_memory_source_readback_digest_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths, _construction_only=True)
    construction += original_native_memory_source_proof_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths, _construction_only=True)
    construction += original_native_memory_source_binding_trace_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths, _construction_only=True)
    construction += original_native_memory_source_vault_consume_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths, _construction_only=True)
    construction += original_native_memory_report_metadata_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths, _construction_only=True)
    if _construction_only:
        return construction
    _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, retention_widths)
    _native_memory_report_authority_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    _native_memory_report_input_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    branches = getattr(record, "_principal_numeric_branches", None)
    reverse_legacy = getattr(record, "_principal_reverse_legacy", None)
    if (type(branches) is not tuple or len(branches) != 3
            or type(branches[0]) is not bool or type(branches[1]) is not bool
            or type(branches[2]) is not bool
            or branches[0] is not True or branches[1] is not False
            or type(reverse_legacy) is not bool):
        raise HeaderBoundsError("memory_source_authority_auth_branch_unavailable")
    cardinality = getattr(record, "_readback_numeric_cardinality", None)
    stage = operation.source.admission.report_source._input_stage
    if (type(cardinality) is not tuple or len(cardinality) != 4
            or cardinality[0] != stage.attempt_id
            or type(cardinality[1]) is not int or not 0 <= cardinality[1] <= MAX_BYTES
            or type(cardinality[2]) is not int or not 0 <= cardinality[2] <= MAX_BYTES
            or type(cardinality[3]) is not int or not 0 <= cardinality[3] <= cardinality[2]):
        raise HeaderBoundsError("memory_source_authority_readback_cardinality_unavailable")
    fields = getattr(record, "_verification_numeric_fields", None)
    if type(fields) is not tuple or len(fields) != 10:
        raise HeaderBoundsError("memory_source_authority_verified_fields_unavailable")
    for field in fields:
        if (type(field) is not tuple or len(field) != 2 or type(field[0]) is not bool
                or type(field[1]) is not int or not 0 <= field[1] <= MAX_BYTES
                or (field[0] is False and field[1] != 0)):
            raise HeaderBoundsError("memory_source_authority_verified_fields_unavailable")
    scan = getattr(record, "_readback_numeric_scan", None)
    if (type(scan) is not tuple or not scan or len(scan) > cardinality[2]
            or type(scan[-1]) is not tuple or len(scan[-1]) != 13
            or scan[-1][0] != 6):
        raise HeaderBoundsError("memory_source_authority_raw_readback_widths_unavailable")
    for index in range(len(scan)):
        row = scan[index]
        if type(row) is not tuple or len(row) != 13:
            raise HeaderBoundsError("memory_source_authority_raw_readback_widths_unavailable")
        for value in row:
            if type(value) is not int or not 0 <= value <= MAX_BYTES:
                raise HeaderBoundsError("memory_source_authority_raw_readback_widths_unavailable")
        if (not 0 <= row[0] <= 6 or not 0 <= row[12] <= 63
                or (row[0] == 6 and index != len(scan) - 1)):
            raise HeaderBoundsError("memory_source_authority_raw_readback_widths_unavailable")
    _native_memory_report_journal_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    _native_memory_source_proof_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    _native_memory_source_binding_numeric_lineage(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    readback_digest_entries = original_native_memory_source_readback_digest_numeric_schedule(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=retention_widths)
    proof_entries = original_native_memory_source_proof_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    binding_trace_entries = original_native_memory_source_binding_trace_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    vault_consume_entries = original_native_memory_source_vault_consume_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    metadata_entries = original_native_memory_report_metadata_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    # These separately prepaid closed children are not a whole-owner result
    # or retained issuance. The proof's nested readback digest is its own
    # original occurrence; the standalone child above belongs to binding.
    # The successful original receipt now supplies its exact raw/scanned
    # numerical branch facts. The original source-binding locator and six
    # pre-REPORT certificate invocations still need their own authentic issuer
    # recurrence, never a REPORT8/common certificate substitution.
    raise HeaderBoundsError("memory_source_authority_source_binding_certificate_trace_unavailable")


def original_native_memory_report_dependency_numeric_schedule(operation, prepared, *, certificate, facts, retention_widths):
    """ONE original REPORT8 issuer; only its own genuine WRS width can grow.

    All metadata/schema/cache appearances come from the exact retained issuer.
    The actual own-row header occurrence is raised from the authentic retained
    original row cost to the original/Current owner maximum. No future cert or
    row is created, and other Source rows retain their observed liabilities.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    own_upper = retention_widths["own_upper"]
    if type(own_upper) is not int or not 0 <= own_upper <= MAX_BYTES:
        raise HeaderBoundsError("memory_report_dependency_numeric_bound")
    exact = record.certificates[4]
    own_id = operation.run.run_identity
    own_cost = None
    for index, identity in enumerate(exact.row_ids):
        if identity == own_id:
            if own_cost is not None:
                raise HeaderBoundsError("memory_report_dependency_numeric_owner_changed")
            own_cost = exact.row_upper_bytes[index]
    if own_cost is None:
        raise HeaderBoundsError("memory_report_dependency_numeric_owner_changed")
    header_upper = exact.upper_bytes - own_cost + max(own_cost, own_upper)
    if header_upper > MAX_BYTES:
        raise HeaderBoundsError("canonical_bound_not_certified")
    entries = []
    matched = 0
    for appearance, amount in record.numeric_trace:
        if type(amount) is not int or not 0 <= amount <= MAX_BYTES:
            raise HeaderBoundsError("memory_report_dependency_numeric_bound")
        if (type(appearance) is tuple and len(appearance) == 4
                and appearance[0] == "exact-header" and appearance[1] == exact.descriptor.table
                and appearance[2] == exact.descriptor.key and appearance[3] == exact.row_ids):
            if amount != exact.upper_bytes:
                raise HeaderBoundsError("memory_report_dependency_numeric_trace_changed")
            amount = header_upper
            matched += 1
        entries.append((appearance, amount))
    if matched != 1:
        raise HeaderBoundsError("memory_report_dependency_numeric_trace_changed")
    # Each future exact issuer occurrence also owns its original prepaid
    # pair/list observation storage; labels and numeric amounts are unchanged.
    from src.memory.composition_headers import _common_trace_label_width
    for index, (appearance, _amount) in enumerate(record.numeric_trace):
        pair_upper = 3 + _common_trace_label_width(appearance) + 20
        entries.append((("native-memory-report-trace", "entry-pair", index), pair_upper))
        entries.append((("native-memory-report-trace", "entry-list-slot", index), pair_upper + 1))
    from src.memory.header_bounds import _native_memory_ascii_string_width as width
    constructor_upper = 2 + 9 + 9 * 4 + 2
    for name in ("db", "budget", "task_row", "issuer_task", "transaction", "driver", "certificates", "guard", "prior", "entries"):
        constructor_upper += width(name) + 1
    entries.extend(((("native-memory-report-trace", "collector-construction"), constructor_upper),
        (("native-memory-report-trace", "collector-weak-reference"), 4),
        (("native-memory-report-trace", "collector-registry-entry"), 27),
        (("native-memory-report-trace", "active-context-token"), 4),
        (("native-memory-report-trace", "trace-context-token"), 4),
        (("native-memory-report-trace", "trace-binding-tuple"), 11),
        (("native-memory-report-trace", "collector-return-tuple"), 16)))
    trace_upper = 2 + max(0, len(record.numeric_trace) - 1)
    for appearance, _amount in record.numeric_trace:
        trace_upper += 3 + _common_trace_label_width(appearance) + 20
    record_upper = 2 + 11 + 6 * 4 + 41 + trace_upper + 20
    for name in ("db", "budget", "issuer_task", "task_id", "owner_principal_id",
            "owner_session_id", "transaction", "driver", "certificates", "numeric_trace", "_seal", "_issued_id"):
        record_upper += width(name) + 1
    record_upper += width(record.task_id) + width(record.owner_principal_id) + width(record.owner_session_id)
    entries.extend(((("native-memory-report-dependencies", "certificate-list"), 41),
        (("native-memory-report-dependencies", "certificate-tuple"), 41),
        (("native-memory-report-trace", "retained-trace-tuple"), trace_upper),
        (("native-memory-report-dependencies", "numeric-inventory"), record_upper),
        (("native-memory-report-dependencies", "numeric-weak-reference"), 4),
        (("native-memory-report-dependencies", "numeric-registry-entry"), 11),
        (("native-memory-report-dependencies", "numeric-frame-slot"),
            3 + width("_native_memory_report_dependency_numeric") + 4)))
    return tuple(entries)


def _native_memory_source_readback_digest_pay(budget, readback):
    """Prepay ONE original _proof_digest without constructing its safe map."""
    from src.memory.header_bounds import _native_memory_ascii_string_width as width, _native_memory_utf8_width
    from src.memory.m5 import _original_memory_vault_nfc_utf8_upper
    raw_values = normalized_values = keys_upper = count = sorted_pairs = 0
    for key in ("receipt_kind", "workflow_run_id", "status", "content_sha256", "readback_id",
            "artifact_id", "verification_id", "verifier_id", "effect_id_digest", "digest", "verified_at"):
        value = readback.get(key)
        if value is None:
            continue
        if type(value) not in (str, bool, int, float):
            raise NativeServiceBlocked("native_memory_readback_numeric_shape_unsupported")
        raw_upper = _native_memory_source_value_upper(value)
        key_upper = width(key)
        keys_upper += key_upper + 1
        raw_values += raw_upper
        count += 1
        sorted_pairs += 3 + key_upper + raw_upper
        _memory_report_stage_pay(budget, "readback-sort-key-str", key_upper)
        _memory_report_stage_pay(budget, "readback-key-str", key_upper)
        _memory_report_stage_pay(budget, "readback-key-nfc-scratch", key_upper)
        _memory_report_stage_pay(budget, "readback-key-nfc-output", key_upper)
        if type(value) is str:
            normalized_upper = 6 * _original_memory_vault_nfc_utf8_upper(_native_memory_utf8_width(value)) + 2
            _memory_report_stage_pay(budget, "readback-value-nfc-scratch", normalized_upper)
            _memory_report_stage_pay(budget, "readback-value-nfc-output", normalized_upper)
        else:
            normalized_upper = raw_upper
        normalized_values += normalized_upper
    punctuation = 2 + max(0, count - 1)
    raw_map = punctuation + keys_upper + raw_values
    normalized_map = punctuation + keys_upper + normalized_values
    _memory_report_stage_pay(budget, "readback-safe-map", raw_map)
    _memory_report_stage_pay(budget, "readback-items-view", 4)
    _memory_report_stage_pay(budget, "readback-sorted-pairs", punctuation + sorted_pairs)
    _memory_report_stage_pay(budget, "readback-normalized-map", normalized_map)
    _memory_report_stage_pay(budget, "readback-canonical", normalized_map)
    _memory_report_stage_pay(budget, "readback-utf8", normalized_map)
    _memory_report_stage_pay(budget, "readback-hexdigest", 66)


def _native_memory_source_proof_scalar_upper(task, attempt, name, *, normalized):
    """Widths of the original closed proof fields, without constructing them."""
    from src.memory.header_bounds import _native_memory_ascii_string_width, _native_memory_utf8_width
    from src.memory.m5 import M5_DIGEST_VERSION, _original_memory_vault_nfc_utf8_upper
    if name == "version":
        value = M5_DIGEST_VERSION
    elif name == "capability_version":
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES
        spec = REGISTERED_CAPABILITIES.get(task.capability_id or "")
        value = spec.version if spec is not None else ""
        if type(value) is int:
            # Original _capability_version produces str(version), not an int.
            return _native_memory_source_value_upper(value) + 2
    elif name == "source_task_id":
        value = task.task_id
    elif name == "source_task_revision":
        value = task.task_revision or 0
    elif name == "source_attempt_id":
        value = attempt.attempt_id
    elif name == "source_attempt_fence":
        value = attempt.fencing_token or 0
    elif name == "workflow_run_id":
        value = attempt.workflow_run_id
    else:
        value = getattr(task, name)
        if name == "goal_revision":
            value = value or 0
        elif name in ("title", "body", "capability_id", "typed_input_digest"):
            value = value or ""
    if type(value) is str:
        if not normalized:
            if name in ("title", "body") and value:
                return 6 * _original_memory_vault_nfc_utf8_upper(_native_memory_utf8_width(value)) + 2
            return _native_memory_ascii_string_width(value)
        utf8_upper = _native_memory_utf8_width(value)
        if name in ("title", "body") and value:
            # The original title/body normalizer ran before this map's NFC.
            utf8_upper = _original_memory_vault_nfc_utf8_upper(utf8_upper)
        return 6 * _original_memory_vault_nfc_utf8_upper(utf8_upper) + 2
    if value is None or type(value) in (int, bool):
        return _native_memory_source_value_upper(value)
    raise NativeServiceBlocked("native_memory_source_proof_numeric_shape_unsupported")


def _native_memory_source_proof_pay(budget, task, attempt, readback):
    """Prepay ONE original _source_proof_from_rows before any of its copies.

    Literal field rosters mirror the three original maps. The input rows and
    readback already belong to the original Source reader; this adds no read.
    """
    from src.memory.header_bounds import _native_memory_ascii_string_width as width, _native_memory_utf8_width
    from src.memory.m5 import _original_memory_vault_nfc_utf8_upper
    refs_count = refs_values = 0
    # The original five-key scan retains each distinct safe ref once. Count
    # duplicates by rescanning the earlier keys, without constructing refs.
    keys = ("readback_id", "artifact_id", "verification_id", "verifier_id", "effect_id_digest")
    for index, key in enumerate(keys):
        value = readback.get(key)
        if not _native_memory_source_safe_ref_numeric(value):
            continue
        duplicate = False
        for prior in range(index):
            if readback.get(keys[prior]) == value:
                duplicate = True
                break
        if not duplicate:
            refs_count += 1
            refs_values += width(value)
    refs_upper = 2 + max(0, refs_count - 1) + refs_values
    _memory_report_stage_pay(budget, "proof-refs-list", refs_upper)
    _memory_report_stage_pay(budget, "proof-refs-slice", refs_upper)
    _native_memory_source_readback_digest_pay(budget, readback)
    # The nested refs list is normalized once more in the evidence digest.
    refs_normalized = 2 + max(0, refs_count - 1) + refs_values
    for index, key in enumerate(keys):
        value = readback.get(key)
        if not _native_memory_source_safe_ref_numeric(value):
            continue
        duplicate = False
        for prior in range(index):
            if readback.get(keys[prior]) == value:
                duplicate = True
                break
        if duplicate:
            continue
        _memory_report_stage_pay(budget, "proof-ref-nfc-scratch", width(value))
        _memory_report_stage_pay(budget, "proof-ref-nfc-output", width(value))
    _memory_report_stage_pay(budget, "proof-normalized-refs-list", refs_normalized)
    nested_upper = 3 + width("refs") + 1 + refs_upper + width("digest") + 1 + 66
    _memory_report_stage_pay(budget, "proof-readback-map", nested_upper)
    _memory_report_stage_pay(budget, "proof-readback-items-view", 4)
    _memory_report_stage_pay(budget, "proof-readback-sorted-pairs", 3 + (3 + width("refs") + refs_upper) + (3 + width("digest") + 66))
    for key in ("refs", "digest"):
        _memory_report_stage_pay(budget, "proof-readback-sort-key-str", width(key))
        _memory_report_stage_pay(budget, "proof-readback-key-str", width(key))
        _memory_report_stage_pay(budget, "proof-readback-key-nfc-scratch", width(key))
        _memory_report_stage_pay(budget, "proof-readback-key-nfc-output", width(key))
    _memory_report_stage_pay(budget, "proof-readback-digest-nfc-scratch", 66)
    _memory_report_stage_pay(budget, "proof-readback-digest-nfc-output", 66)
    _memory_report_stage_pay(budget, "proof-normalized-readback-map", nested_upper)
    for kind in ("intent", "context", "evidence"):
        if kind == "intent":
            names = ("version", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id", "capability_version", "typed_input_digest", "title", "body", "handoff_ids")
        elif kind == "context":
            names = ("version", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "title", "body")
        else:
            names = ("version", "owner_principal_id", "owner_session_id", "source_task_id", "source_task_revision", "source_attempt_id", "source_attempt_fence", "workflow_run_id", "goal_id", "goal_revision", "capability_id", "capability_version", "typed_input_digest", "readback")
        raw_map = normalized_map = 2 + len(names) - 1
        sorted_pairs = 2 + len(names) - 1
        for name in names:
            if name == "handoff_ids":
                raw_upper = normalized_upper = 2
                _memory_report_stage_pay(budget, "proof-empty-handoffs-sorted-list", 2)
                _memory_report_stage_pay(budget, "proof-empty-handoffs-normalized-list", 2)
            elif name == "readback":
                raw_upper = normalized_upper = nested_upper
            else:
                raw_upper = _native_memory_source_proof_scalar_upper(task, attempt, name, normalized=False)
                normalized_upper = _native_memory_source_proof_scalar_upper(task, attempt, name, normalized=True)
                if name in ("title", "body") and getattr(task, name):
                    text = getattr(task, name)
                    _memory_report_stage_pay(budget, "proof-text-crlf-replace", width(text))
                    _memory_report_stage_pay(budget, "proof-text-cr-replace", width(text))
                    _memory_report_stage_pay(budget, "proof-text-nfc-scratch", raw_upper)
                    _memory_report_stage_pay(budget, "proof-text-nfc-output", raw_upper)
                    _memory_report_stage_pay(budget, "proof-text-strip", raw_upper)
                if name in ("capability_id", "capability_version", "typed_input_digest"):
                    _memory_report_stage_pay(budget, "proof-field-str", raw_upper)
                    if name == "capability_version":
                        _memory_report_stage_pay(budget, "proof-capability-lookup-str", width(task.capability_id or ""))
                # Only string fields allocate NFC scratch/output in the
                # original canonical mapper; the fixed field types decide it.
                if (name not in ("goal_revision", "source_task_revision", "source_attempt_fence")
                        and not (name in ("owner_principal_id", "owner_session_id", "goal_id")
                            and getattr(task, name) is None)):
                    _memory_report_stage_pay(budget, "proof-value-nfc-scratch", normalized_upper)
                    _memory_report_stage_pay(budget, "proof-value-nfc-output", normalized_upper)
            key_upper = width(name)
            raw_map += key_upper + 1 + raw_upper
            normalized_map += key_upper + 1 + normalized_upper
            sorted_pairs += 3 + key_upper + raw_upper
            _memory_report_stage_pay(budget, "proof-sort-key-str", key_upper)
            _memory_report_stage_pay(budget, "proof-key-str", key_upper)
            _memory_report_stage_pay(budget, "proof-key-nfc-scratch", key_upper)
            _memory_report_stage_pay(budget, "proof-key-nfc-output", key_upper)
        _memory_report_stage_pay(budget, "proof-map", raw_map)
        _memory_report_stage_pay(budget, "proof-items-view", 4)
        _memory_report_stage_pay(budget, "proof-sorted-pairs", sorted_pairs)
        _memory_report_stage_pay(budget, "proof-normalized-map", normalized_map)
        _memory_report_stage_pay(budget, "proof-canonical", normalized_map)
        _memory_report_stage_pay(budget, "proof-utf8", normalized_map)
        _memory_report_stage_pay(budget, "proof-hexdigest", 66)
    version_upper = _native_memory_source_proof_scalar_upper(task, attempt, "capability_version", normalized=False)
    _memory_report_stage_pay(budget, "proof-final-capability-lookup-str", width(task.capability_id or ""))
    _memory_report_stage_pay(budget, "proof-final-capability-version-str", version_upper)
    copy_upper = _native_memory_source_value_upper(readback)
    _memory_report_stage_pay(budget, "proof-readback-copy", copy_upper)
    constructor_upper = 2 + 7 + 3 * 4 + copy_upper + 3 * 66 + version_upper
    for name in ("task", "attempt", "run", "readback", "evidence_digest", "source_context_digest", "task_intent_digest", "capability_version"):
        constructor_upper += width(name) + 1
    _memory_report_stage_pay(budget, "proof-constructor-kwargs", constructor_upper)
    _memory_report_stage_pay(budget, "proof-constructor", constructor_upper)
    _native_memory_source_proof_numeric_observe(budget, task, attempt, readback)



def _native_memory_report_metadata_label_numeric_upper(value):
    """Original metadata label codec, without generator/container allocation."""
    from src.memory.header_bounds import HeaderBoundsError
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise HeaderBoundsError("memory_report_metadata_label_unavailable")
        return 20
    if type(value) is str:
        upper = 2
        for char in value:
            code = ord(char)
            upper += (2 if char in '\"\\' else 6 if code < 32 or 127 <= code <= 65535
                else 12 if code > 65535 else 1)
        return upper
    if type(value) is tuple:
        upper = 2 + max(0, len(value) - 1)
        for item in value:
            upper += _native_memory_report_metadata_label_numeric_upper(item)
        return upper
    raise HeaderBoundsError("memory_report_metadata_label_unavailable")


def original_native_memory_report_metadata_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE successful original metadata trace and its exact observer storage.

    The original owner issues the trace only after all metadata checks succeed.
    Nested Runtime/auth/candidate/parser payments stay in their observed order;
    the separate Source proof outside that lexical region is not included.
    Scalar mode prices this helper's new labels/pairs/list/tuple, not body reads.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_report_metadata_constructor_mode_changed")
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    lineage = _native_memory_report_metadata_numeric_lineage(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    from src.security.site_policy import (_memory_policy_dimensions_match,
        _memory_policy_dimensions_valid, settings)
    policy = getattr(lineage[0], "_site_policy_numeric_inputs", None)
    if (type(policy) is not tuple or len(policy) != 3 or type(policy[0]) is not tuple
            or not 1 <= len(policy[0]) <= 8
            or not _memory_policy_dimensions_match(settings.browser_site_allowlist, policy[1])
            or not _memory_policy_dimensions_match(settings.browser_site_blocklist, policy[2])):
        raise HeaderBoundsError("memory_report_metadata_policy_changed")
    for dimensions in policy[0]:
        if not _memory_policy_dimensions_valid(dimensions):
            raise HeaderBoundsError("memory_report_metadata_policy_changed")
    # The getter validates the privately issued record, original input stage,
    # same current DB/TX/driver/Task/frame, four Source certs and policy facts.
    # No copied tuples, missing success trace or caller-defined rows qualify.
    trace = lineage[8]
    if type(trace) is not tuple or not trace:
        raise HeaderBoundsError("memory_report_metadata_trace_unavailable")
    prefix_upper = _native_memory_report_metadata_label_numeric_upper("native-memory-report-trace")
    constructor_upper = 2 + 9 + 9 * 4 + 2
    for name in ("db", "budget", "task_row", "issuer_task", "transaction", "driver",
            "certificates", "guard", "prior", "entries"):
        constructor_upper += _native_memory_report_metadata_label_numeric_upper(name) + 1
    entries = None if _construction_only else []
    construction = pair_values = count = 0
    # Original first three issuer payments, the two private mode slots and
    # four-cert tuple21, then the original four context/return payments.
    for stage in ("collector-construction", "collector-weak-reference", "collector-registry-entry",
            "metadata-record-slot", "metadata-input-stage-slot", "metadata-certificates-tuple",
            "active-context-token", "trace-context-token", "trace-binding-tuple", "collector-return-tuple"):
        if stage == "collector-construction":
            upper = constructor_upper
        elif stage == "collector-registry-entry":
            upper = 27
        elif stage == "trace-binding-tuple":
            upper = 11
        elif stage == "collector-return-tuple":
            upper = 16
        elif stage == "metadata-record-slot":
            upper = 3 + _native_memory_report_metadata_label_numeric_upper("_metadata_record") + 4
        elif stage == "metadata-input-stage-slot":
            upper = 3 + _native_memory_report_metadata_label_numeric_upper("_metadata_input_stage") + 4
        elif stage == "metadata-certificates-tuple":
            upper = 21
        else:
            upper = 4
        label_upper = 3 + prefix_upper + _native_memory_report_metadata_label_numeric_upper(stage)
        pair_upper = 3 + label_upper + 20
        construction += label_upper + pair_upper
        pair_values += pair_upper
        count += 1
        if not _construction_only:
            entries.append((("native-memory-report-trace", stage), upper))
    core_upper = 2 + max(0, len(trace) - 1)
    for index in range(len(trace)):
        entry = trace[index]
        if (type(entry) is not tuple or len(entry) != 2 or type(entry[1]) is not int
                or not 0 <= entry[1] <= MAX_BYTES):
            raise HeaderBoundsError("memory_report_metadata_trace_unavailable")
        observed_pair = 3 + _native_memory_report_metadata_label_numeric_upper(entry[0]) + 20
        core_upper += observed_pair
        # append_charge prepays pair/list storage before the original debit.
        # Its numeric index is a signed-int64 encoded scalar, not a new ref.
        for stage in ("entry-pair", "entry-list-slot"):
            upper = observed_pair + (1 if stage == "entry-list-slot" else 0)
            label_upper = 2 + 2 + prefix_upper + _native_memory_report_metadata_label_numeric_upper(stage) + 20
            pair_upper = 3 + label_upper + 20
            construction += label_upper + pair_upper
            pair_values += pair_upper
            count += 1
            if not _construction_only:
                entries.append((("native-memory-report-trace", stage, index), upper))
        # Reuse the SAME original pair, never rebuild or suppress its charge.
        pair_values += observed_pair
        count += 1
        if not _construction_only:
            entries.append(entry)
    lineage_upper = 2 + 8 + 7 * 4 + 21 + core_upper
    for stage in ("metadata-retained-core-tuple", "metadata-retained-lineage", "metadata-record-slot"):
        if stage == "metadata-retained-core-tuple":
            upper = core_upper
        elif stage == "metadata-retained-lineage":
            upper = lineage_upper
        else:
            upper = 3 + _native_memory_report_metadata_label_numeric_upper("_report_metadata_numeric_trace") + 4
        label_upper = 2 + 2 + prefix_upper + _native_memory_report_metadata_label_numeric_upper(stage) + 20
        pair_upper = 3 + label_upper + 20
        construction += label_upper + pair_upper
        pair_values += pair_upper
        count += 1
        if not _construction_only:
            entries.append((("native-memory-report-trace", stage, len(trace)), upper))
    # The list and returned immutable tuple are distinct encoded containers.
    construction += 2 * (2 + max(0, count - 1) + pair_values)
    if _construction_only:
        return construction
    return tuple(entries)


def original_native_memory_source_proof_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE original proof pay vector; scalar mode prepays its constructors only."""
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width as width
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_source_proof_constructor_mode_changed")
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    observed = _native_memory_source_proof_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    record = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    child_constructor = original_native_memory_source_readback_digest_numeric_schedule(
        operation, prepared, certificate=certificate, facts=facts,
        retention_widths=retention_widths, _construction_only=True)
    fields = record._source_readback_digest_numeric_fields
    entries = None if _construction_only else []
    constructor = child_constructor
    pair_values = events = 0
    prefix_upper = width("native-memory-report-stage")
    scalars = observed[2]
    refs = observed[3]
    refs_upper = 2 + max(0, len(refs) - 1)
    for value in refs:
        refs_upper += value
    for stage in ("proof-refs-list", "proof-refs-slice"):
        upper = refs_upper
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    for field in fields:
        if not field[0]:
            continue
        for stage in ("readback-sort-key-str", "readback-key-str",
                "readback-key-nfc-scratch", "readback-key-nfc-output"):
            pair_values += 3 + (3 + prefix_upper + width(stage)) + 20
            events += 1
        if field[1]:
            for stage in ("readback-value-nfc-scratch", "readback-value-nfc-output"):
                pair_values += 3 + (3 + prefix_upper + width(stage)) + 20
                events += 1
    for stage in ("readback-safe-map", "readback-items-view", "readback-sorted-pairs",
            "readback-normalized-map", "readback-canonical", "readback-utf8", "readback-hexdigest"):
        pair_values += 3 + (3 + prefix_upper + width(stage)) + 20
        events += 1
    if not _construction_only:
        child = original_native_memory_source_readback_digest_numeric_schedule(
            operation, prepared, certificate=certificate, facts=facts,
            retention_widths=retention_widths)
        entries.extend(child)
    for value in refs:
        for stage in ("proof-ref-nfc-scratch", "proof-ref-nfc-output"):
            upper = value
            if not 0 <= upper <= MAX_BYTES:
                raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
            label_upper = 3 + prefix_upper + width(stage)
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-report-stage", stage), upper))
    stage = "proof-normalized-refs-list"
    upper = refs_upper
    if not 0 <= upper <= MAX_BYTES:
        raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
    label_upper = 3 + prefix_upper + width(stage)
    pair_upper = 3 + label_upper + 20
    constructor += label_upper + pair_upper
    pair_values += pair_upper
    events += 1
    if not _construction_only:
        entries.append((("native-memory-report-stage", stage), upper))
    nested_upper = 3 + width("refs") + 1 + refs_upper + width("digest") + 1 + 66
    for stage in ("proof-readback-map", "proof-readback-items-view", "proof-readback-sorted-pairs"):
        upper = (4 if stage == "proof-readback-items-view" else
            3 + (3 + width("refs") + refs_upper) + (3 + width("digest") + 66)
            if stage == "proof-readback-sorted-pairs" else nested_upper)
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    for key in ("refs", "digest"):
        for stage in ("proof-readback-sort-key-str", "proof-readback-key-str",
                "proof-readback-key-nfc-scratch", "proof-readback-key-nfc-output"):
            upper = width(key)
            if not 0 <= upper <= MAX_BYTES:
                raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
            label_upper = 3 + prefix_upper + width(stage)
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-report-stage", stage), upper))
    for stage in ("proof-readback-digest-nfc-scratch", "proof-readback-digest-nfc-output",
            "proof-normalized-readback-map"):
        upper = nested_upper if stage == "proof-normalized-readback-map" else 66
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    for kind in ("intent", "context", "evidence"):
        if kind == "intent":
            names = ("version", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id", "capability_version", "typed_input_digest", "title", "body", "handoff_ids")
        elif kind == "context":
            names = ("version", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "title", "body")
        else:
            names = ("version", "owner_principal_id", "owner_session_id", "source_task_id", "source_task_revision", "source_attempt_id", "source_attempt_fence", "workflow_run_id", "goal_id", "goal_revision", "capability_id", "capability_version", "typed_input_digest", "readback")
        raw_map = normalized_map = sorted_pairs = 2 + len(names) - 1
        for name in names:
            if name == "handoff_ids":
                raw_upper = normalized_upper = 2
                for stage in ("proof-empty-handoffs-sorted-list", "proof-empty-handoffs-normalized-list"):
                    upper = 2
                    if not 0 <= upper <= MAX_BYTES:
                        raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
                    label_upper = 3 + prefix_upper + width(stage)
                    pair_upper = 3 + label_upper + 20
                    constructor += label_upper + pair_upper
                    pair_values += pair_upper
                    events += 1
                    if not _construction_only:
                        entries.append((("native-memory-report-stage", stage), upper))
            elif name == "readback":
                raw_upper = normalized_upper = nested_upper
            else:
                field = scalars[name]
                raw_upper, normalized_upper = field[0], field[1]
                if name in ("title", "body") and field[4]:
                    for stage in ("proof-text-crlf-replace", "proof-text-cr-replace",
                            "proof-text-nfc-scratch", "proof-text-nfc-output", "proof-text-strip"):
                        upper = field[2] if stage in ("proof-text-crlf-replace", "proof-text-cr-replace") else raw_upper
                        if not 0 <= upper <= MAX_BYTES:
                            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
                        label_upper = 3 + prefix_upper + width(stage)
                        pair_upper = 3 + label_upper + 20
                        constructor += label_upper + pair_upper
                        pair_values += pair_upper
                        events += 1
                        if not _construction_only:
                            entries.append((("native-memory-report-stage", stage), upper))
                if name in ("capability_id", "capability_version", "typed_input_digest"):
                    stage = "proof-field-str"
                    upper = raw_upper
                    if not 0 <= upper <= MAX_BYTES:
                        raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
                    label_upper = 3 + prefix_upper + width(stage)
                    pair_upper = 3 + label_upper + 20
                    constructor += label_upper + pair_upper
                    pair_values += pair_upper
                    events += 1
                    if not _construction_only:
                        entries.append((("native-memory-report-stage", stage), upper))
                    if name == "capability_version":
                        stage = "proof-capability-lookup-str"
                        upper = scalars["capability_id"][0]
                        if not 0 <= upper <= MAX_BYTES:
                            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
                        label_upper = 3 + prefix_upper + width(stage)
                        pair_upper = 3 + label_upper + 20
                        constructor += label_upper + pair_upper
                        pair_values += pair_upper
                        events += 1
                        if not _construction_only:
                            entries.append((("native-memory-report-stage", stage), upper))
                if field[3]:
                    for stage in ("proof-value-nfc-scratch", "proof-value-nfc-output"):
                        upper = normalized_upper
                        if not 0 <= upper <= MAX_BYTES:
                            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
                        label_upper = 3 + prefix_upper + width(stage)
                        pair_upper = 3 + label_upper + 20
                        constructor += label_upper + pair_upper
                        pair_values += pair_upper
                        events += 1
                        if not _construction_only:
                            entries.append((("native-memory-report-stage", stage), upper))
            key_upper = width(name)
            raw_map += key_upper + 1 + raw_upper
            normalized_map += key_upper + 1 + normalized_upper
            sorted_pairs += 3 + key_upper + raw_upper
            for stage in ("proof-sort-key-str", "proof-key-str", "proof-key-nfc-scratch", "proof-key-nfc-output"):
                upper = key_upper
                if not 0 <= upper <= MAX_BYTES:
                    raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
                label_upper = 3 + prefix_upper + width(stage)
                pair_upper = 3 + label_upper + 20
                constructor += label_upper + pair_upper
                pair_values += pair_upper
                events += 1
                if not _construction_only:
                    entries.append((("native-memory-report-stage", stage), upper))
        for stage in ("proof-map", "proof-items-view", "proof-sorted-pairs",
                "proof-normalized-map", "proof-canonical", "proof-utf8", "proof-hexdigest"):
            upper = (raw_map if stage == "proof-map" else 4 if stage == "proof-items-view"
                else sorted_pairs if stage == "proof-sorted-pairs" else 66 if stage == "proof-hexdigest"
                else normalized_map)
            if not 0 <= upper <= MAX_BYTES:
                raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
            label_upper = 3 + prefix_upper + width(stage)
            pair_upper = 3 + label_upper + 20
            constructor += label_upper + pair_upper
            pair_values += pair_upper
            events += 1
            if not _construction_only:
                entries.append((("native-memory-report-stage", stage), upper))
    constructor_upper = 2 + 7 + 3 * 4 + observed[4] + 3 * 66 + observed[5]
    for name in ("task", "attempt", "run", "readback", "evidence_digest", "source_context_digest", "task_intent_digest", "capability_version"):
        constructor_upper += width(name) + 1
    for stage in ("proof-final-capability-lookup-str", "proof-final-capability-version-str",
            "proof-readback-copy", "proof-constructor-kwargs", "proof-constructor"):
        upper = (scalars["capability_id"][0] if stage == "proof-final-capability-lookup-str"
            else observed[5] if stage == "proof-final-capability-version-str"
            else observed[4] if stage == "proof-readback-copy" else constructor_upper)
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    scalar_names = ("version", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id",
        "capability_version", "typed_input_digest", "title", "body", "source_task_id", "source_task_revision",
        "source_attempt_id", "source_attempt_fence", "workflow_run_id")
    map_upper = 2 + len(scalar_names) - 1
    for name in scalar_names:
        map_upper += width(name) + 1 + 76
    stage = "proof-numeric-scalar-map"
    upper = map_upper
    if not 0 <= upper <= MAX_BYTES:
        raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
    label_upper = 3 + prefix_upper + width(stage)
    pair_upper = 3 + label_upper + 20
    constructor += label_upper + pair_upper
    pair_values += pair_upper
    events += 1
    if not _construction_only:
        entries.append((("native-memory-report-stage", stage), upper))
    for name in scalar_names:
        stage = "proof-numeric-scalar-tuple"
        upper = 76
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    numeric_refs_upper = 2 + max(0, len(refs) - 1) + 20 * len(refs)
    retained_upper = 2 + 5 + width(observed[0]) + width(observed[1]) + map_upper + numeric_refs_upper + 20 + 20
    digest_fields_upper = 2 + 10 + 11 * 55
    for stage in ("proof-numeric-scalar-proxy", "proof-numeric-ref-list", "proof-numeric-ref-tuple",
            "proof-numeric-retained-tuple", "proof-numeric-record-slot", "proof-readback-numeric-field-list"):
        upper = (4 if stage == "proof-numeric-scalar-proxy" else
            numeric_refs_upper if stage in ("proof-numeric-ref-list", "proof-numeric-ref-tuple") else
            retained_upper if stage == "proof-numeric-retained-tuple" else
            3 + width("_source_proof_numeric_dimensions") + 4 if stage == "proof-numeric-record-slot" else digest_fields_upper)
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    for index in range(11):
        stage = "proof-readback-numeric-field-pair"
        upper = 55
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    for stage in ("proof-readback-numeric-field-tuple", "proof-readback-numeric-field-slot"):
        upper = (digest_fields_upper if stage == "proof-readback-numeric-field-tuple" else
            3 + width("_source_readback_digest_numeric_fields") + 4)
        if not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_upper_unavailable")
        label_upper = 3 + prefix_upper + width(stage)
        pair_upper = 3 + label_upper + 20
        constructor += label_upper + pair_upper
        pair_values += pair_upper
        events += 1
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    # Child pairs/labels are already priced by child_constructor; its SAME
    # objects occupy the separately paid outer list and final tuple too.
    constructor += 2 * (2 + max(0, events - 1) + pair_values)
    if _construction_only:
        return constructor
    return tuple(entries)


def _native_memory_source_safe_ref_numeric(value):
    """Original SAFE_ID grammar, scalar sizing without a regex match object."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return False
    for character in value:
        codepoint = ord(character)
        if not (65 <= codepoint <= 90 or 97 <= codepoint <= 122
                or 48 <= codepoint <= 57 or character in "_.:/-"):
            return False
    return True


def _native_memory_source_proof_numeric_observe(budget, task, attempt, readback):
    """Retain only original proof scalar capacities on the actual REPORT owner."""
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width as width
    from src.work_board.review import (_MemoryReportDependencyNumericInventory,
        _MEMORY_REPORT_DEPENDENCY_NUMERIC, _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL,
        _memory_report_row_certificate)
    record = getattr(budget, "_native_memory_report_dependency_numeric", None)
    if (type(record) is not _MemoryReportDependencyNumericInventory
            or _MEMORY_REPORT_DEPENDENCY_NUMERIC.get(record) is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL):
        raise HeaderBoundsError("memory_source_proof_numeric_owner_changed")
    task_certificate = _memory_report_row_certificate(record.db, task, budget, "work_board_tasks")
    attempt_certificate = _memory_report_row_certificate(record.db, task, budget, "work_board_attempts")
    if (task.task_id not in task_certificate.row_ids or attempt.attempt_id not in attempt_certificate.row_ids
            or attempt.task_id != task.task_id or attempt.workflow_run_id is None):
        raise HeaderBoundsError("memory_source_proof_numeric_owner_changed")
    names = ("version", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id",
        "capability_version", "typed_input_digest", "title", "body", "source_task_id", "source_task_revision",
        "source_attempt_id", "source_attempt_fence", "workflow_run_id")
    map_upper = 2 + len(names) - 1
    for name in names:
        map_upper += width(name) + 1 + 76
    _memory_report_stage_pay(budget, "proof-numeric-scalar-map", map_upper)
    scalars = {}
    for name in names:
        raw = _native_memory_source_proof_scalar_upper(task, attempt, name, normalized=False)
        normalized = _native_memory_source_proof_scalar_upper(task, attempt, name, normalized=True)
        # All original transformed fixed scalar fields are strings except the
        # three original revision/fence fields and genuinely nullable IDs.
        string_value = name not in ("goal_revision", "source_task_revision", "source_attempt_fence")
        if name in ("owner_principal_id", "owner_session_id", "goal_id") and getattr(task, name) is None:
            string_value = False
        text_nonempty = name in ("title", "body") and bool(getattr(task, name))
        original_text = width(getattr(task, name) or "") if name in ("title", "body") else 0
        _memory_report_stage_pay(budget, "proof-numeric-scalar-tuple", 76)
        scalars[name] = (raw, normalized, original_text, string_value, text_nonempty)
    _memory_report_stage_pay(budget, "proof-numeric-scalar-proxy", 4)
    scalars = MappingProxyType(scalars)
    ref_count = 0
    keys = ("readback_id", "artifact_id", "verification_id", "verifier_id", "effect_id_digest")
    for index in range(len(keys)):
        value = readback.get(keys[index])
        if not _native_memory_source_safe_ref_numeric(value):
            continue
        duplicate = False
        for earlier in range(index):
            if readback.get(keys[earlier]) == value:
                duplicate = True
                break
        ref_count += not duplicate
    ref_upper = 2 + max(0, ref_count - 1) + 20 * ref_count
    _memory_report_stage_pay(budget, "proof-numeric-ref-list", ref_upper)
    refs = []
    for index in range(len(keys)):
        value = readback.get(keys[index])
        if not _native_memory_source_safe_ref_numeric(value):
            continue
        duplicate = False
        for earlier in range(index):
            if readback.get(keys[earlier]) == value:
                duplicate = True
                break
        if not duplicate:
            refs.append(width(value))
    _memory_report_stage_pay(budget, "proof-numeric-ref-tuple", ref_upper)
    refs = tuple(refs)
    retained_upper = (2 + 5 + width(task.task_id) + width(attempt.attempt_id)
        + map_upper + ref_upper + 20 + 20)
    _memory_report_stage_pay(budget, "proof-numeric-retained-tuple", retained_upper)
    retained = (task.task_id, attempt.attempt_id, scalars, refs,
        _native_memory_source_value_upper(readback),
        _native_memory_source_proof_scalar_upper(task, attempt, "capability_version", normalized=False))
    _memory_report_stage_pay(budget, "proof-numeric-record-slot",
        3 + width("_source_proof_numeric_dimensions") + 4)
    object.__setattr__(record, "_source_proof_numeric_dimensions", retained)
    # The canonical readback digest has its own scalar appearances. The
    # original proof-copy total cannot identify their presence/type/NFC
    # branches. Observe only numbers from this same already-read value.
    digest_fields_upper = 2 + 10 + 11 * 55
    _memory_report_stage_pay(budget, "proof-readback-numeric-field-list", digest_fields_upper)
    digest_fields = []
    for name in ("receipt_kind", "workflow_run_id", "status", "content_sha256", "readback_id",
            "artifact_id", "verification_id", "verifier_id", "effect_id_digest", "digest", "verified_at"):
        value = readback.get(name)
        present = value is not None
        string_value = type(value) is str
        raw = _native_memory_source_value_upper(value) if present else 0
        if string_value:
            from src.memory.header_bounds import _native_memory_utf8_width
            from src.memory.m5 import _original_memory_vault_nfc_utf8_upper
            normalized = 6 * _original_memory_vault_nfc_utf8_upper(_native_memory_utf8_width(value)) + 2
        else:
            normalized = raw
        _memory_report_stage_pay(budget, "proof-readback-numeric-field-pair", 55)
        digest_fields.append((present, string_value, raw, normalized))
    _memory_report_stage_pay(budget, "proof-readback-numeric-field-tuple", digest_fields_upper)
    digest_fields = tuple(digest_fields)
    _memory_report_stage_pay(budget, "proof-readback-numeric-field-slot",
        3 + width("_source_readback_digest_numeric_fields") + 4)
    object.__setattr__(record, "_source_readback_digest_numeric_fields", digest_fields)


def _native_memory_source_candidate_text_pay(budget, proof):
    """ONE original structured candidate, including its repeated refs scan."""
    from src.memory.header_bounds import _native_memory_ascii_string_width as width, _native_memory_utf8_width
    from src.memory.m5 import _original_memory_vault_nfc_utf8_upper
    ref = proof.evidence_digest
    found = False
    refs_count = refs_values = 0
    keys = ("readback_id", "artifact_id", "verification_id", "verifier_id", "effect_id_digest")
    for index, key in enumerate(keys):
        value = proof.readback.get(key)
        if not _native_memory_source_safe_ref_numeric(value):
            continue
        if not found:
            ref = value
            found = True
        duplicate = False
        for prior in range(index):
            if proof.readback.get(keys[prior]) == value:
                duplicate = True
                break
        if not duplicate:
            refs_count += 1
            refs_values += width(value)
    refs_upper = 2 + max(0, refs_count - 1) + refs_values
    _memory_report_stage_pay(budget, "candidate-refs-list", refs_upper)
    _memory_report_stage_pay(budget, "candidate-refs-slice", refs_upper)
    # The selected REPORT Source has exact string IDs and a typed integer
    # revision. Decimal digit width is already available without str(revision).
    if (type(proof.task.capability_id) is not str or type(proof.task.goal_id) is not str
            or type(proof.task.goal_revision) is not int):
        raise NativeServiceBlocked("native_memory_source_candidate_numeric_shape_unsupported")
    utf8_upper = (_native_memory_utf8_width("Verified  completed successfully for goal  at revision ; independent readback  is bound to the source attempt.")
        + _native_memory_utf8_width(proof.task.capability_id)
        + _native_memory_utf8_width(proof.task.goal_id)
        + _native_memory_source_value_upper(proof.task.goal_revision)
        + _native_memory_utf8_width(ref))
    raw_upper = 6 * utf8_upper + 2
    normalized_upper = 6 * _original_memory_vault_nfc_utf8_upper(utf8_upper) + 2
    _memory_report_stage_pay(budget, "candidate-revision-format", _native_memory_source_value_upper(proof.task.goal_revision) + 2)
    _memory_report_stage_pay(budget, "candidate-format", raw_upper)
    _memory_report_stage_pay(budget, "candidate-crlf-replace", raw_upper)
    _memory_report_stage_pay(budget, "candidate-cr-replace", raw_upper)
    _memory_report_stage_pay(budget, "candidate-nfc-scratch", normalized_upper)
    _memory_report_stage_pay(budget, "candidate-nfc-output", normalized_upper)
    _memory_report_stage_pay(budget, "candidate-strip", normalized_upper)


def original_native_memory_source_candidate_text_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE original candidate-text payment; scalar prepays its nine pairs."""
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    from src.memory.m5 import _original_memory_vault_nfc_utf8_upper
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_source_candidate_text_constructor_mode_changed")
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    observed = _native_memory_source_proof_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    scalars = observed[2]
    capability = scalars["capability_id"]
    goal = scalars["goal_id"]
    revision = scalars["goal_revision"]
    if (capability[3] is not True or goal[3] is not True or revision[3] is not False
            or capability[0] < 2 or goal[0] < 2 or revision[0] != 20):
        raise HeaderBoundsError("memory_source_candidate_text_shape_unavailable")
    refs = observed[3]
    refs_upper = 2 + max(0, len(refs) - 1)
    for ref_upper in refs:
        # Original _SAFE_ID accepts only 1..512 ASCII characters. Its exact
        # JSON string width is byte length+2, unlike general UTF capacities.
        if not 3 <= ref_upper <= 514:
            raise HeaderBoundsError("memory_source_candidate_text_shape_unavailable")
        refs_upper += ref_upper
    ref_utf8 = refs[0] - 2 if refs else 64
    # The proof observer's raw capability/Goal strings use ensure_ascii JSON
    # widths, NOT 6*UTF+2. JSON width-2 bounds their original UTF8 safely;
    # division by six would underprice ordinary ASCII identifiers.
    utf8_upper = (len("Verified  completed successfully for goal  at revision ; independent readback  is bound to the source attempt.")
        + capability[0] - 2 + goal[0] - 2 + revision[0] + ref_utf8)
    raw_upper = 6 * utf8_upper + 2
    normalized_upper = 6 * _original_memory_vault_nfc_utf8_upper(utf8_upper) + 2
    if raw_upper > MAX_BYTES or normalized_upper > MAX_BYTES:
        raise HeaderBoundsError("memory_source_candidate_text_bound")
    entries = None if _construction_only else []
    construction = pair_values = 0
    prefix_upper = len("native-memory-report-stage") + 2
    for stage in ("candidate-refs-list", "candidate-refs-slice", "candidate-revision-format",
            "candidate-format", "candidate-crlf-replace", "candidate-cr-replace",
            "candidate-nfc-scratch", "candidate-nfc-output", "candidate-strip"):
        if stage in ("candidate-refs-list", "candidate-refs-slice"):
            upper = refs_upper
        elif stage == "candidate-revision-format":
            upper = revision[0] + 2
        elif stage in ("candidate-format", "candidate-crlf-replace", "candidate-cr-replace"):
            upper = raw_upper
        else:
            upper = normalized_upper
        # Prefix/stage names are literal ASCII without escapes. No JSON,
        # generator, label or forecast container is built in scalar mode.
        label_upper = 3 + prefix_upper + len(stage) + 2
        pair_upper = 3 + label_upper + 20
        construction += label_upper + pair_upper
        pair_values += pair_upper
        if not _construction_only:
            entries.append((("native-memory-report-stage", stage), upper))
    construction += 2 * (2 + 8 + pair_values)
    if _construction_only:
        return construction
    return tuple(entries)


def _native_memory_source_prepared_text_model_pay(budget, prepared_text):
    """Original three-field kwargs and dataclass, before ** mapping expansion."""
    upper = _native_memory_source_value_upper(prepared_text)
    _memory_report_stage_pay(budget, "prepared-text-kwargs", upper)
    _memory_report_stage_pay(budget, "prepared-text-constructor", upper)


def _native_memory_source_binding_model_pay(budget, task, proof):
    """Original closed fourteen-field model; no sizing model_dump or copy."""
    from src.memory.header_bounds import _native_memory_ascii_string_width as width
    values_upper = (_native_memory_source_value_upper(task.task_id)
        + _native_memory_source_value_upper(task.task_revision)
        + _native_memory_source_value_upper(proof.attempt.attempt_id)
        + _native_memory_source_value_upper(proof.attempt.fencing_token)
        + _native_memory_source_value_upper(proof.attempt.workflow_run_id)
        + _native_memory_source_value_upper(proof.run.revision)
        + _native_memory_source_value_upper(task.goal_id)
        + _native_memory_source_value_upper(task.goal_revision)
        + _native_memory_source_value_upper(task.capability_id)
        + _native_memory_source_value_upper(proof.capability_version) + 4 * 66)
    upper = 2 + 13 + values_upper
    for name in ("task_id", "expected_task_revision", "attempt_id", "attempt_fence", "workflow_run_id",
            "workflow_run_revision", "goal_id", "goal_revision", "capability_id", "capability_version",
            "typed_input_digest", "source_context_digest", "evidence_digest", "readback_digest"):
        upper += width(name) + 1
    _memory_report_stage_pay(budget, "binding-model-kwargs", upper)
    _memory_report_stage_pay(budget, "binding-model-validation", upper)
    _memory_report_stage_pay(budget, "binding-model-dump", upper)
    _native_memory_source_readback_digest_pay(budget, proof.readback)


def _native_memory_source_binding_delivery_pay(budget, certificate, identity, stage, datetime_fields):
    """Original closed ORM row delivery, independent of its read permission."""
    if budget is None:
        return
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, HeaderCertificate, _SEAL
    from src.extensions.capability_execution import _configured_mac_numeric_frame
    frame = _configured_mac_numeric_frame()
    if (type(budget) is not HeaderReadBudget or (frame is not None and frame is not budget)
            or type(certificate) is not HeaderCertificate or getattr(certificate, "_seal", None) is not _SEAL
            or getattr(certificate, "_issued_id", None) != id(certificate)
            or type(certificate.row_ids) is not tuple or len(certificate.row_ids) != 1
            or certificate.row_ids[0] != identity
            or type(certificate.row_upper_bytes) is not tuple or len(certificate.row_upper_bytes) != 1
            or certificate._transaction is not certificate._db.sync_session.get_transaction()
            or not certificate._driver.in_transaction
            or certificate._changes != certificate._driver.total_changes):
        raise HeaderBoundsError("memory_source_binding_delivery_numeric_owner_changed")
    # The actual existing PK certificate supplies the original row's encoded
    # field sum. The mapped datetime constructor has the same fixed44 capacity
    # used by the reviewed original REPORT lookup payer; nullable fields need
    # no additional body read to reserve that finite original column capacity.
    if stage == "task":
        model_label, date_label = "source-binding-task-model-delivery", "source-binding-task-date-delivery"
    elif stage == "goal":
        model_label, date_label = "source-binding-goal-model-delivery", "source-binding-goal-date-delivery"
    elif stage == "attempt":
        model_label, date_label = "source-binding-attempt-model-delivery", "source-binding-attempt-date-delivery"
    elif stage == "run":
        model_label, date_label = "source-binding-run-model-delivery", "source-binding-run-date-delivery"
    else:
        raise HeaderBoundsError("memory_source_binding_delivery_numeric_owner_changed")
    _memory_report_stage_pay(budget, model_label, certificate.row_upper_bytes[0])
    _memory_report_stage_pay(budget, date_label, datetime_fields * 44)


async def source_binding(db, *, principal_id, session_id, task_id, revision, attempt_id, header_budget=None,
        _report_input_stage=None, _report_source=None, _numeric_source=None):
    from src.db.models import Goal, WorkBoardTask
    from src.memory import m5
    from sqlalchemy import text
    from src.memory.header_bounds import (
        HeaderReadBudget, HeaderBoundsError, WORK_BOARD_TASK, WORK_BOARD_ATTEMPT,
        GOAL, WRS_BY_RUN, validate_certificate,
    )
    budget = header_budget if header_budget is not None else HeaderReadBudget()
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    connection = await db.connection()
    driver = (await connection.get_raw_connection()).driver_connection
    if not driver.in_transaction:
        # The preparatory phase needs a real read snapshot on this exact
        # connection. It remains separate from the later original writer.
        await db.execute(text("BEGIN"))
    from src.memory.composition_headers import locate_exact_rows
    pending_numeric_trace = None
    source_collector = source_active_token = source_trace_token = None
    if _numeric_source is not None:
        _native_memory_report_source_capacity_checked(_numeric_source, db, budget)
        if (_numeric_source.report_source is not _report_source
                or _report_source._input_stage.task_id != task_id
                or _report_source._input_stage.attempt_id != attempt_id
                or _numeric_source.operator.principal.principal_id != principal_id
                or _numeric_source.operator.session_id != session_id):
            raise HeaderBoundsError("memory_report_source_resource_owner_changed")
        source_collector, source_active_token, source_trace_token = _begin_memory_report_source_numeric_trace(
            db, _numeric_source, budget, "source-binding")
    try:
        # Certify the exact original source rows before even their bounded
        # relationship projections. The later full-body appearance is charged
        # separately below on this same frame.
        try:
            for descriptor, identity in ((WORK_BOARD_TASK, task_id), (WORK_BOARD_ATTEMPT, attempt_id)):
                if not await locate_exact_rows(db, descriptor, identity, budget,
                        _header_budget=budget if source_collector is not None else None):
                    raise NativeServiceBlocked("native_memory_original_source_changed")
                initial_certificate = await budget.certify(db, descriptor, (identity,),
                    _header_budget=budget if source_collector is not None else None)
                if source_collector is not None:
                    _checked_memory_report_source_trace_certificate(source_collector, initial_certificate, descriptor, identity)
        except HeaderBoundsError as error:
            raise NativeServiceBlocked("native_memory_source_bound_not_certified") from error
        # Locators are selected without Task/Attempt JSON or free text. Every full
        # source row is certified before the first ORM body/proof reader.
        if source_collector is not None:
            # Original local function construction is a logical Source object;
            # numeric payment adds no relationship/body read permission.
            _memory_report_stage_pay(budget, "source-binding-projection-function", 32)
        def bounded(column):
            return (f"CASE WHEN typeof({column})='text' THEN CASE WHEN "
                    f"octet_length({column}) BETWEEN 1 AND 512 THEN {column} END END")
        if source_collector is not None:
            from src.memory.header_bounds import _native_memory_ascii_string_width as width
            # This literal is the exact original bounded expression. It sizes
            # the new f-string before bounded() or the outer SELECT runs.
            expression_upper = width("CASE WHEN typeof(goal_id)='text' THEN CASE WHEN octet_length(goal_id) BETWEEN 1 AND 512 THEN goal_id END END")
            _memory_report_stage_pay(budget, "source-binding-goal-expression", expression_upper)
            _memory_report_stage_pay(budget, "source-binding-goal-query", expression_upper
                + len("SELECT ") + len(" FROM work_board_tasks WHERE task_id=:id LIMIT 1"))
            _memory_report_stage_pay(budget, "source-binding-goal-parameters", 3 + width("id") + width(task_id))
            _memory_report_stage_pay(budget, "source-binding-goal-projection-delivery", 6 * 512 + 2)
        goal_id = await db.scalar(text(f"SELECT {bounded('goal_id')} FROM work_board_tasks "
                                      "WHERE task_id=:id LIMIT 1"), {"id": task_id})
        if source_collector is not None:
            expression_upper = width("CASE WHEN typeof(workflow_run_id)='text' THEN CASE WHEN octet_length(workflow_run_id) BETWEEN 1 AND 512 THEN workflow_run_id END END")
            _memory_report_stage_pay(budget, "source-binding-run-expression", expression_upper)
            _memory_report_stage_pay(budget, "source-binding-run-query", expression_upper
                + len("SELECT ") + len(" FROM work_board_attempts WHERE attempt_id=:id LIMIT 1"))
            _memory_report_stage_pay(budget, "source-binding-run-parameters", 3 + width("id") + width(attempt_id))
            _memory_report_stage_pay(budget, "source-binding-run-projection-delivery", 6 * 512 + 2)
        run_id = await db.scalar(text(f"SELECT {bounded('workflow_run_id')} FROM work_board_attempts "
                                     "WHERE attempt_id=:id LIMIT 1"), {"id": attempt_id})
        if type(goal_id) is not str or type(run_id) is not str:
            raise NativeServiceBlocked("native_memory_original_source_changed")
        certificates = []
        try:
            for descriptor, identity in ((WORK_BOARD_TASK, task_id), (WORK_BOARD_ATTEMPT, attempt_id),
                                         (GOAL, goal_id), (WRS_BY_RUN, run_id)):
                certificates.append(await budget.certify(db, descriptor, (identity,),
                    _header_budget=budget if source_collector is not None else None))
            for certificate in certificates:
                await validate_certificate(db, certificate)
        except HeaderBoundsError as error:
            raise NativeServiceBlocked("native_memory_source_bound_not_certified") from error
        if source_collector is not None:
            for index in range(4):
                descriptor = WORK_BOARD_TASK if index == 0 else WORK_BOARD_ATTEMPT if index == 1 else GOAL if index == 2 else WRS_BY_RUN
                identity = task_id if index == 0 else attempt_id if index == 1 else goal_id if index == 2 else run_id
                _checked_memory_report_source_trace_certificate(source_collector, certificates[index], descriptor, identity)
            pending_numeric_trace = _finish_memory_report_source_numeric_trace(source_collector)
    finally:
        if source_collector is not None:
            _end_memory_report_numeric_trace(source_collector, source_active_token, source_trace_token)
    _native_memory_source_binding_delivery_pay(header_budget, certificates[0], task_id, "task", 7)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
    if (task is None or task.owner_principal_id != principal_id or task.owner_session_id != session_id
        or task.task_revision != revision):
        raise NativeServiceBlocked("native_memory_original_source_changed")
    if task.capability_id not in METADATA_SOURCE_CAPABILITIES | {REPORT_SOURCE_CAPABILITY}:
        # Other existing source owners reopen private files or require their
        # own staged source token. Their legacy routes retain that behavior.
        raise NativeServiceBlocked("native_memory_source_profile_unsupported")
    _native_memory_source_binding_delivery_pay(header_budget, certificates[2], task.goal_id, "goal", 4)
    goal = await db.get(Goal, task.goal_id)
    if (goal is None or goal.owner_principal_id != principal_id or goal.owner_session_id != session_id
        or goal.revision != task.goal_revision or goal.status != "active"):
        raise NativeServiceBlocked("native_memory_original_goal_changed")
    if task.capability_id == REPORT_SOURCE_CAPABILITY:
        from src.db.models import WorkBoardAttempt, WorkflowRunState
        from src.work_board.review import native_report_memory_metadata, _stage_terminal_report_input
        _native_memory_source_binding_delivery_pay(header_budget, certificates[1], attempt_id, "attempt", 7)
        attempt = await db.get(WorkBoardAttempt, attempt_id, populate_existing=True)
        _native_memory_source_binding_delivery_pay(header_budget, certificates[3], run_id, "run", 6)
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity ==
            (attempt.workflow_run_id if attempt is not None else "")).execution_options(populate_existing=True))
        if attempt is None or run is None:
            raise NativeServiceBlocked("native_memory_original_source_changed")
        if _report_source is not None:
            if (type(_report_source) is not NativeMemoryReportSourceWitness
                    or type(_report_source._identity) is not _MemoryReportSourceIssuance
                    or _report_source._identity.source_identity != id(_report_source)
                    or _report_source._identity.content_digest != _memory_report_source_digest(_report_source)):
                raise NativeServiceBlocked("native_memory_original_report_witness_required")
            stage = _report_source._input_stage
            _checked_memory_report_input_stage(stage, budget, task_id, attempt_id, preparing=False)
        else:
            stage = _report_input_stage
            if stage is None:
                if db.info.get("native_writer_started"):
                    raise NativeServiceBlocked("native_memory_original_report_witness_required")
                payload, witness, workspace, operation = await _stage_terminal_report_input(
                    db, task, header_budget=budget, _original_source_certificates=certificates)
                stage = _issue_memory_report_input_stage(
                    budget, task_id, attempt_id, payload, witness, workspace, operation)
            _checked_memory_report_input_stage(stage, budget, task_id, attempt_id, preparing=True)
        readback = await native_report_memory_metadata(db, task, attempt, run,
            _report_input_bytes=stage.input_bytes, _report_source_witness=stage.source_witness,
            _report_workspace_identity=stage.workspace_bytes,
            _report_operation_token=stage.operation_token, header_budget=budget,
            _original_source_certificates=certificates, _memory_report_input_stage=stage)
        _native_memory_source_proof_pay(budget, task, attempt, readback)
        proof = m5._source_proof_from_rows(task, attempt, run, readback)
        from src.memory.header_bounds import _native_memory_ascii_string_width
        _memory_report_stage_pay(budget, "proof-stage-slot",
            3 + _native_memory_ascii_string_width("_native_memory_report_input_stage") + 4)
        object.__setattr__(proof, "_native_memory_report_input_stage", stage)
    else:
        proof = await m5._verified_source(db, task, requested_attempt_id=attempt_id)
    if pending_numeric_trace is not None:
        from src.memory.header_bounds import _native_memory_ascii_string_width
        _memory_report_stage_pay(budget, "source-binding-pending-proof-slot",
            3 + _native_memory_ascii_string_width("_source_binding_numeric_pending") + 4)
        object.__setattr__(proof, "_source_binding_numeric_pending", pending_numeric_trace)
    if task.capability_id == REPORT_SOURCE_CAPABILITY:
        _native_memory_source_binding_model_pay(budget, task, proof)
    return MemorySourceBinding(task_id=task.task_id, expected_task_revision=task.task_revision,
        attempt_id=proof.attempt.attempt_id, attempt_fence=proof.attempt.fencing_token,
        workflow_run_id=proof.attempt.workflow_run_id, workflow_run_revision=proof.run.revision,
        goal_id=task.goal_id, goal_revision=task.goal_revision, capability_id=task.capability_id,
        capability_version=proof.capability_version, typed_input_digest=task.typed_input_digest,
        source_context_digest=proof.source_context_digest, evidence_digest=proof.evidence_digest,
        readback_digest=m5._proof_digest(proof.readback)).model_dump(mode="json"), proof


@dataclass(frozen=True)
class NativeMemoryReportSourceWitness:
    """Original private staging identity, not a serializable action grant."""
    candidate_digest: str
    row_tokens: tuple
    proof_json: str
    output_reference: str
    output_digest: str
    byte_count: int
    typed_input_json: str
    _input_stage: object = field(default=None, repr=False, compare=False)
    _identity: object = field(default=None, init=False, repr=False, compare=False)
    _consumed: bool = field(default=False, init=False, repr=False, compare=False)


@dataclass(frozen=True)
class _MemoryReportSourceIssuance:
    source_identity: int
    content_digest: str


@dataclass(frozen=True)
class _OriginalMemoryReportInputStage:
    """Actual original physical inputs; never a serialized authority token."""
    budget: object = field(repr=False)
    issuer_task: object = field(repr=False)
    task_id: str
    attempt_id: str
    input_bytes: bytes = field(repr=False)
    source_witness: object = field(repr=False)
    workspace_bytes: bytes = field(repr=False)
    operation_token: str
    _identity: object = field(default=None, repr=False, compare=False)


_MEMORY_REPORT_INPUT_STAGES = weakref.WeakValueDictionary()


@dataclass(frozen=True, eq=False, repr=False)
class _OriginalMemoryReportInputNumericFacts:
    stage: object
    inventory: object
    task_id: str
    input_digest: str
    input_upper: int
    dimensions: tuple
    _seal: object = field(default=None, init=False)
    _issued_id: int = field(default=0, init=False)


_MEMORY_REPORT_INPUT_NUMERIC_SEAL = object()
_MEMORY_REPORT_INPUT_NUMERIC = weakref.WeakValueDictionary()


def _memory_report_decoder_numeric_owner(stage, task, payload, db, budget):
    """Existing physical stage and exact REPORT8 resource lineage only."""
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
    from src.db.models import WorkBoardTask
    from sqlalchemy.ext.asyncio import AsyncSession
    from src.work_board.review import (_MemoryReportDependencyNumericInventory,
        _MEMORY_REPORT_DEPENDENCY_NUMERIC, _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL)
    inventory = getattr(budget, "_native_memory_report_dependency_numeric", None)
    frame = _native_memory_source_numeric_frame()
    if (type(stage) is not _OriginalMemoryReportInputStage
            or _MEMORY_REPORT_INPUT_STAGES.get(id(stage)) is not stage
            or type(stage._identity) is not _MemoryReportSourceIssuance
            or stage._identity.source_identity != id(stage)
            or stage.budget is not budget or type(budget) is not HeaderReadBudget
            or stage.issuer_task is not asyncio.current_task()
            or type(task) is not WorkBoardTask or type(db) is not AsyncSession
            or stage.task_id != task.task_id or payload is not stage.input_bytes
            or task.capability_id != REPORT_SOURCE_CAPABILITY
            or type(inventory) is not _MemoryReportDependencyNumericInventory
            or _MEMORY_REPORT_DEPENDENCY_NUMERIC.get(inventory) is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL
            or inventory._seal is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL
            or inventory._issued_id != id(inventory) or inventory.db is not db
            or inventory.budget is not budget or inventory.task_id != task.task_id
            or inventory.owner_principal_id != task.owner_principal_id
            or inventory.owner_session_id != task.owner_session_id
            or inventory.issuer_task is not asyncio.current_task()
            or inventory.transaction is not db.sync_session.get_transaction()
            or not inventory.driver.in_transaction
            or type(inventory.certificates) is not tuple or len(inventory.certificates) != 8
            or (frame is not None and frame is not budget)):
        raise HeaderBoundsError("memory_report_input_numeric_owner_changed")
    from src.memory.header_bounds import HeaderCertificate, _SEAL
    for certificate in inventory.certificates:
        if (type(certificate) is not HeaderCertificate or certificate._seal is not _SEAL
                or certificate._issued_id != id(certificate) or certificate._db is not db
                or certificate._transaction is not inventory.transaction
                or certificate._driver is not inventory.driver
                or certificate._changes != inventory.driver.total_changes):
            raise HeaderBoundsError("memory_report_input_numeric_owner_changed")
    from sqlalchemy.engine import Connection
    bound = False
    for connection in inventory.transaction._connections:
        if type(connection) is Connection:
            entry = inventory.transaction._connections[connection]
            if (entry[0] is connection and entry[1] is connection.get_transaction()
                    and connection.in_transaction()
                    and connection.connection.driver_connection is inventory.driver):
                bound = True
    if not bound:
        raise HeaderBoundsError("memory_report_input_numeric_owner_changed")
    return inventory




def _memory_report_stage_pay(budget, stage, upper):
    from src.memory.header_bounds import HeaderReadBudget
    frame = _native_memory_source_numeric_frame()
    if type(budget) is not HeaderReadBudget or (frame is not None and frame is not budget):
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    budget.debit(upper, appearance=("native-memory-report-stage", stage))


def _native_memory_report_authority_numeric_facts(operation, prepared, *, certificate, facts, retention_widths):
    """Borrow the successful original REPORT owner's numerical field maps."""
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    inventory = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    stage = operation.source.admission.report_source._input_stage
    observed = getattr(inventory, "_authority_numeric_facts", None)
    if (type(observed) is not tuple or len(observed) != 8
            or observed[0] != stage.task_id or observed[1] != stage.attempt_id
            or type(observed[2]) is not str or not observed[2]
            or observed[2] == operation.run.run_identity
            or type(observed[7]) is not str or len(observed[7]) != 64):
        raise HeaderBoundsError("memory_report_authority_numeric_owner_changed")
    for index in (3, 4, 5, 6):
        mapping = observed[index]
        if type(mapping) is not MappingProxyType:
            raise HeaderBoundsError("memory_report_authority_numeric_shape_unavailable")
        for name, amount in mapping.items():
            if (type(name) is not str or type(amount) is not int
                    or not 0 <= amount <= MAX_BYTES):
                raise HeaderBoundsError("memory_report_authority_numeric_shape_unavailable")
    if (observed[0] not in inventory.certificates[2].row_ids
            or observed[1] not in inventory.certificates[3].row_ids
            or observed[2] not in inventory.certificates[4].row_ids):
        raise HeaderBoundsError("memory_report_authority_numeric_owner_changed")
    return observed


def _native_memory_report_input_numeric_facts(operation, prepared, *, certificate, facts, retention_widths):
    """Borrow only the original decoder's authenticated numerical record."""
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    inventory = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    stage = operation.source.admission.report_source._input_stage
    record = getattr(stage, "_numeric_inputs", None)
    if (type(record) is not _OriginalMemoryReportInputNumericFacts
            or _MEMORY_REPORT_INPUT_NUMERIC.get(id(record)) is not record
            or record._seal is not _MEMORY_REPORT_INPUT_NUMERIC_SEAL or record._issued_id != id(record)
            or record.stage is not stage or record.inventory is not inventory
            or record.task_id != stage.task_id
            or type(record.input_upper) is not int or not 0 <= record.input_upper <= MAX_BYTES
            or type(record.dimensions) is not tuple or len(record.dimensions) != 10):
        raise HeaderBoundsError("memory_report_input_numeric_owner_changed")
    observed = _native_memory_report_authority_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    if observed[7] != record.input_digest:
        raise HeaderBoundsError("memory_report_input_numeric_owner_changed")
    index = 0
    for name in ("schema_version", "operation_ref", "plan_version", "producer_task_ref", "producer_attempt_ref",
            "handoff_ref", "producer_sha256", "producer_schema", "quoted_source_data", "no_learning"):
        dimension = record.dimensions[index]
        if (type(dimension) is not tuple or len(dimension) != 3 or dimension[0] != name
                or type(dimension[1]) is not int or not 0 <= dimension[1] <= MAX_BYTES
                or type(dimension[2]) is not int or not 0 <= dimension[2] <= MAX_BYTES):
            raise HeaderBoundsError("memory_report_input_numeric_shape_unavailable")
        index += 1
    return record



def _native_memory_report_journal_numeric_facts(operation, prepared, *, certificate, facts, retention_widths):
    """Borrow this original REPORT reader's authenticated numeric dimensions."""
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    from src.runtime_plugins.task_capability import _ADMISSION_FIELDS, REPORT_CHECKPOINT_IDS
    inventory = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    authority = _native_memory_report_authority_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    observed = getattr(inventory, "_report_journal_numeric", None)
    if (type(observed) is not tuple or len(observed) != 9 or observed[0] != authority[2]
            or type(observed[1]) is not MappingProxyType or len(observed[1]) != len(_ADMISSION_FIELDS)
            or type(observed[2]) is not tuple or not 1 <= len(observed[2]) <= 50
            or type(observed[3]) is not int or not 0 <= observed[3] <= MAX_BYTES
            or type(observed[4]) is not int or not 0 <= observed[4] <= MAX_BYTES):
        raise HeaderBoundsError("memory_report_journal_numeric_owner_changed")
    for index in (5, 6, 7, 8):
        if type(observed[index]) is not int or not 0 <= observed[index] <= MAX_BYTES:
            raise HeaderBoundsError("memory_report_journal_numeric_shape_unavailable")
    for name in observed[1]:
        pair = observed[1][name]
        if name not in _ADMISSION_FIELDS or type(pair) is not tuple or len(pair) != 2:
            raise HeaderBoundsError("memory_report_journal_numeric_shape_unavailable")
        for value in pair:
            if type(value) is not int or not 0 <= value <= MAX_BYTES:
                raise HeaderBoundsError("memory_report_journal_numeric_shape_unavailable")
    for row in observed[2]:
        if type(row) is not tuple or len(row) != 9:
            raise HeaderBoundsError("memory_report_journal_numeric_shape_unavailable")
        for value in row:
            if type(value) is not int or not 0 <= value <= MAX_BYTES:
                raise HeaderBoundsError("memory_report_journal_numeric_shape_unavailable")
        if not 0 <= row[5] <= len(REPORT_CHECKPOINT_IDS) or not 0 <= row[7] <= 34:
            raise HeaderBoundsError("memory_report_journal_numeric_shape_unavailable")
    return observed



def _native_memory_source_proof_numeric_facts(operation, prepared, *, certificate, facts, retention_widths):
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    inventory = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    authority = _native_memory_report_authority_numeric_facts(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    observed = getattr(inventory, "_source_proof_numeric_dimensions", None)
    names = ("version", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id",
        "capability_version", "typed_input_digest", "title", "body", "source_task_id", "source_task_revision",
        "source_attempt_id", "source_attempt_fence", "workflow_run_id")
    if (type(observed) is not tuple or len(observed) != 6
            or observed[0] != authority[0] or observed[1] != authority[1]
            or type(observed[2]) is not MappingProxyType or len(observed[2]) != len(names)
            or type(observed[3]) is not tuple or len(observed[3]) > 5
            or type(observed[4]) is not int or not 0 <= observed[4] <= MAX_BYTES
            or type(observed[5]) is not int or not 0 <= observed[5] <= MAX_BYTES):
        raise HeaderBoundsError("memory_source_proof_numeric_owner_changed")
    for name in names:
        row = observed[2].get(name)
        if (type(row) is not tuple or len(row) != 5
                or type(row[0]) is not int or not 0 <= row[0] <= MAX_BYTES
                or type(row[1]) is not int or not 0 <= row[1] <= MAX_BYTES
                or type(row[2]) is not int or not 0 <= row[2] <= MAX_BYTES
                or type(row[3]) is not bool or type(row[4]) is not bool
                or (name not in ("title", "body") and (row[2] != 0 or row[4]))):
            raise HeaderBoundsError("memory_source_proof_numeric_shape_unavailable")
    for value in observed[3]:
        if type(value) is not int or not 0 <= value <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_proof_numeric_shape_unavailable")
    return observed


def _memory_report_input_stage_digest(stage):
    from src.memory.header_bounds import _native_memory_utf8_width
    budget = stage.budget
    _memory_report_stage_pay(budget, "input-hash-return", 66)
    input_digest = hashlib.sha256(stage.input_bytes).hexdigest()
    _memory_report_stage_pay(budget, "workspace-hash-return", 66)
    workspace_digest = hashlib.sha256(stage.workspace_bytes).hexdigest()
    # Size the literal original payload without constructing a widths dict or
    # the canonical payload first. The same arithmetic pays every recheck.
    from src.memory.header_bounds import _native_memory_ascii_string_width
    payload_upper = (2 + 7 + 8
        + _native_memory_ascii_string_width("task") + _native_memory_source_value_upper(stage.task_id)
        + _native_memory_ascii_string_width("attempt") + _native_memory_source_value_upper(stage.attempt_id)
        + _native_memory_ascii_string_width("input") + 66
        + _native_memory_ascii_string_width("workspace") + 66
        + _native_memory_ascii_string_width("operation") + _native_memory_source_value_upper(stage.operation_token)
        + _native_memory_ascii_string_width("source") + 4
        + _native_memory_ascii_string_width("input_size") + _native_memory_source_value_upper(len(stage.input_bytes))
        + _native_memory_ascii_string_width("workspace_size") + _native_memory_source_value_upper(len(stage.workspace_bytes)))
    _memory_report_stage_pay(budget, "identity-payload", payload_upper)
    value = {"task": stage.task_id, "attempt": stage.attempt_id, "input": input_digest,
        "workspace": workspace_digest, "operation": stage.operation_token, "source": None,
        "input_size": len(stage.input_bytes), "workspace_size": len(stage.workspace_bytes)}
    _memory_report_stage_pay(budget, "identity-canonical", payload_upper)
    material = _canonical(value)
    _memory_report_stage_pay(budget, "identity-utf8", _native_memory_utf8_width(material))
    encoded = material.encode("utf-8")
    _memory_report_stage_pay(budget, "identity-digest-return", 66)
    return hashlib.sha256(encoded).hexdigest()


def _issue_memory_report_input_stage(budget, task_id, attempt_id, payload, witness, workspace, operation):
    import asyncio
    from src.memory.header_bounds import _native_memory_ascii_string_width
    task = asyncio.current_task()
    if (task is None or type(payload) is not bytes or type(workspace) is not bytes
            or witness is not None or type(operation) is not str):
        raise NativeServiceBlocked("native_memory_source_profile_unsupported")
    constructor_upper = (2 + 8 + 9
        + _native_memory_ascii_string_width("budget") + 4
        + _native_memory_ascii_string_width("issuer_task") + 4
        + _native_memory_ascii_string_width("task_id") + _native_memory_source_value_upper(task_id)
        + _native_memory_ascii_string_width("attempt_id") + _native_memory_source_value_upper(attempt_id)
        + _native_memory_ascii_string_width("input_bytes") + 6 * len(payload) + 2
        + _native_memory_ascii_string_width("source_witness") + 4
        + _native_memory_ascii_string_width("workspace_bytes") + 6 * len(workspace) + 2
        + _native_memory_ascii_string_width("operation_token") + _native_memory_source_value_upper(operation)
        + _native_memory_ascii_string_width("_identity") + 4)
    _memory_report_stage_pay(budget, "stage-constructor", constructor_upper)
    stage = _OriginalMemoryReportInputStage(budget, task, task_id, attempt_id, payload, witness, workspace, operation)
    digest = _memory_report_input_stage_digest(stage)
    issuance_upper = (2 + 1 + 2 + _native_memory_ascii_string_width("source_identity")
        + _native_memory_source_value_upper(id(stage))
        + _native_memory_ascii_string_width("content_digest") + 66)
    _memory_report_stage_pay(budget, "stage-issuance", issuance_upper)
    object.__setattr__(stage, "_identity", _MemoryReportSourceIssuance(id(stage), digest))
    _memory_report_stage_pay(budget, "stage-weak-reference", 4)
    _memory_report_stage_pay(budget, "stage-registry-entry", 3 + _native_memory_source_value_upper(id(stage)) + 4)
    _MEMORY_REPORT_INPUT_STAGES[id(stage)] = stage
    return stage


def _checked_memory_report_input_stage(stage, budget, task_id, attempt_id, *, preparing):
    import asyncio
    if (type(stage) is not _OriginalMemoryReportInputStage or stage.budget is not budget
            or _MEMORY_REPORT_INPUT_STAGES.get(id(stage)) is not stage
            or stage.task_id != task_id or stage.attempt_id != attempt_id
            or type(stage.input_bytes) is not bytes or type(stage.workspace_bytes) is not bytes
            or type(stage.operation_token) is not str
            or stage.source_witness is not None
            or (preparing and stage.issuer_task is not asyncio.current_task())
            or type(stage._identity) is not _MemoryReportSourceIssuance
            or stage._identity.source_identity != id(stage)
            or stage._identity.content_digest != _memory_report_input_stage_digest(stage)):
        raise NativeServiceBlocked("native_memory_original_report_witness_required")
    return stage


def _memory_report_source_digest(source):
    from src.memory.header_bounds import _native_memory_ascii_string_width, _native_memory_utf8_width
    if source._input_stage is not None:
        stage = source._input_stage
        if (type(stage) is not _OriginalMemoryReportInputStage
                or _MEMORY_REPORT_INPUT_STAGES.get(id(stage)) is not stage
                or type(stage.input_bytes) is not bytes or type(stage.workspace_bytes) is not bytes
                or type(stage.operation_token) is not str):
            raise NativeServiceBlocked("native_memory_original_report_witness_required")
        fields_upper = 2 + 6 + 7
        for name in ("candidate_digest", "row_tokens", "proof_json", "output_reference",
                "output_digest", "byte_count", "typed_input_json"):
            fields_upper += _native_memory_ascii_string_width(name) + _native_memory_source_value_upper(getattr(source, name))
        _memory_report_stage_pay(stage.budget, "source-digest-fields", fields_upper)
    fields = {name: getattr(source, name) for name in ("candidate_digest", "row_tokens", "proof_json",
        "output_reference", "output_digest", "byte_count", "typed_input_json")}
    if source._input_stage is not None:
        stage_digest = _memory_report_input_stage_digest(stage)
        fields_upper += 2 + _native_memory_ascii_string_width("original_input_stage") + 66
        _memory_report_stage_pay(stage.budget, "source-digest-stage-entry", fields_upper)
        fields["original_input_stage"] = stage_digest
        _memory_report_stage_pay(stage.budget, "source-digest-canonical", fields_upper)
        material = _canonical(fields)
        _memory_report_stage_pay(stage.budget, "source-digest-utf8", _native_memory_utf8_width(material))
        encoded = material.encode("utf-8")
        _memory_report_stage_pay(stage.budget, "source-digest-return", 66)
        return hashlib.sha256(encoded).hexdigest()
    return _digest(fields)


@dataclass(frozen=True)
class _MemoryReportReadContext:
    source: NativeMemoryReportSourceWitness
    admission: NativeMemoryMutationAdmission
    writer: object
    authority_check: object


async def _memory_report_rows(db, proof, *, header_budget):
    from src.db.models import WorkBoardInputArtifact, Goal
    from src.work_board.pipelines import row_token
    task, attempt, run = proof.task, proof.attempt, proof.run
    from src.memory.header_bounds import INPUT_ARTIFACT, GOAL
    await header_budget.certify(db, INPUT_ARTIFACT, (task.input_artifact_id,))
    await header_budget.certify(db, GOAL, (task.goal_id,))
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
    goal = await db.get(Goal, task.goal_id, populate_existing=True)
    if artifact is None or goal is None:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    return tuple((row.__tablename__, str(getattr(row, "task_id", None) if row is task else
        getattr(row, "attempt_id", None) if row is attempt else getattr(row, "run_identity", None)
        if row is run else getattr(row, "artifact_id", None) if row is artifact else row.id), row_token(row))
        for row in (task, attempt, run, artifact, goal))


async def stage_memory_report_source(db, admission, *, _report_input_stage=None):
    """Actual source/input/file reads occur before admission BEGIN."""
    if db.info.get("native_writer_started"):
        raise NativeServiceBlocked("native_memory_original_ingress_required")
    value = admission.candidate()
    if value["method"] == "memory.forget":
        return admission
    old = value["source"]
    if old["capability_id"] != REPORT_SOURCE_CAPABILITY:
        raise NativeServiceBlocked("native_memory_source_profile_unsupported")
    current, proof = await source_binding(db, principal_id=value["operator_principal_id"],
        session_id=value["operator_session_id"], task_id=old["task_id"],
        revision=old["expected_task_revision"], attempt_id=old["attempt_id"], header_budget=admission.header_budget,
        _report_input_stage=_report_input_stage)
    if current != old:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    from src.work_board.dispatcher import _decode_typed_input_payload
    from src.work_board.pipeline_cpu import read_output
    from src.runtime_plugins.task_capability import read_report_candidate, REPORT_CHECKPOINT_IDS, _journal
    metadata = read_report_candidate(proof.run)
    invoked = next(item["payload"] for item in _journal(proof.run.checkpoint_receipts_json)
        if item["checkpoint_id"] == REPORT_CHECKPOINT_IDS[2])
    stage = proof._native_memory_report_input_stage
    inputs = _decode_typed_input_payload(proof.task, stage.input_bytes)
    from src.workflows.job_runtime import _digest as input_digest
    from src.work_board.pipeline_contracts import EvidenceConsumerInput
    handoffs = json.loads(proof.attempt.parent_handoff_context_json)
    safe_inputs = {"input": EvidenceConsumerInput.model_validate(dict(inputs)).model_dump(mode="json"),
        "parent_handoff_context": handoffs, "parent_handoff_digest": proof.attempt.parent_handoff_digest}
    if input_digest(safe_inputs) != metadata["input_digest"]:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    raw = read_output(invoked["output_reference"], invoked["output_sha256"], max_bytes=65536,
                      header_budget=admission.header_budget)
    if len(raw) != invoked["size_bytes"]:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    source = NativeMemoryReportSourceWitness(admission.candidate_digest,
        await _memory_report_rows(db, proof, header_budget=admission.header_budget),
        _canonical(proof.readback), invoked["output_reference"], invoked["output_sha256"], len(raw), _canonical(inputs),
        _input_stage=stage)
    object.__setattr__(source, "_identity", _MemoryReportSourceIssuance(id(source), _memory_report_source_digest(source)))
    return replace(admission, report_source=source)


async def recheck_memory_report_source(db, task, attempt, context, *, read_current):
    """One adopted 64KiB private report read; no other writer file access."""
    if (type(context) is not _MemoryReportReadContext or context.writer is not db
        or type(context.source) is not NativeMemoryReportSourceWitness
        or type(context.source._identity) is not _MemoryReportSourceIssuance
        or context.source._identity.source_identity != id(context.source)
        or context.source._identity.content_digest != _memory_report_source_digest(context.source)
        or context.admission.report_source is not context.source
        or context.source.candidate_digest != context.admission.candidate_digest):
        raise NativeServiceBlocked("native_memory_original_report_witness_required")
    source = context.source
    value = context.admission.candidate()
    old = value["source"]
    if (task.task_id != old["task_id"] or attempt.attempt_id != old["attempt_id"]
        or old["capability_id"] != REPORT_SOURCE_CAPABILITY):
        raise NativeServiceBlocked("native_memory_original_source_changed")
    await context.authority_check(db)
    numeric_source = _memory_report_source_for_revalidation(db, context.admission)
    current, proof = await source_binding(db, principal_id=value["operator_principal_id"],
        session_id=value["operator_session_id"], task_id=old["task_id"],
        revision=old["expected_task_revision"], attempt_id=old["attempt_id"],
        header_budget=context.admission.header_budget, _report_source=source, _numeric_source=numeric_source)
    if (current != old or await _memory_report_rows(db, proof,
            header_budget=context.admission.header_budget) != source.row_tokens
        or _canonical(proof.readback) != source.proof_json
        or datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"])):
        raise NativeServiceBlocked("native_memory_original_source_changed")
    if read_current:
        if source._consumed or not db.info.get("native_writer_started") or not db.in_transaction():
            raise NativeServiceBlocked("native_memory_original_report_witness_required")
        object.__setattr__(source, "_consumed", True)
        if not source.output_reference.startswith("artifacts/work-board/evidence/"):
            raise NativeServiceBlocked("native_memory_source_profile_unsupported")
        from src.work_board.pipeline_cpu import read_output
        import time
        began = time.monotonic()
        try:
            raw = read_output(source.output_reference, source.output_digest, max_bytes=65536,
                              header_budget=context.admission.header_budget)
        except (ValueError, OSError) as exc:
            raise NativeServiceBlocked("native_memory_report_readback_changed") from exc
        finally:
            db.info["native_memory_report_read_seconds"] = time.monotonic() - began
        if len(raw) != source.byte_count:
            raise NativeServiceBlocked("native_memory_report_readback_changed")
        await context.authority_check(db)
        if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
            raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    _memory_report_stage_pay(context.admission.header_budget, "recheck-readback-return-copy",
        _native_memory_source_value_upper(proof.readback))
    _commit_memory_report_source_numeric_trace(numeric_source, proof, recheck=True, read_current=read_current)
    return dict(proof.readback)


async def _prepare_original_memory_text(db, value, budget):
    from src.memory import m5
    from src.memory.header_bounds import SECRET
    # The existing owner reads the complete Vault inventory before redaction,
    # in readonly redaction, and again to bind the unchanged inventory.
    for _ in range(3):
        await budget.certify_all(db, SECRET)
    prepared = await m5.prepare_m5_text(db, value, header_budget=budget)
    from src.memory.header_bounds import _native_memory_ascii_string_width
    # Every original caller immediately asdict()s this genuine staged result.
    # Size its already-owned closed fields without building the sizing dict.
    upper = (2 + 2 + 3 + 132 + _native_memory_ascii_string_width(prepared.sanitized_text)
        + _native_memory_ascii_string_width("original_digest")
        + _native_memory_ascii_string_width("sanitized_text")
        + _native_memory_ascii_string_width("vault_rows_digest"))
    _memory_report_stage_pay(budget, "prepared-text-initial-asdict", upper)
    return prepared


async def _consume_original_memory_text(db, value, prepared, budget):
    from src.memory import m5
    guard = db.info.get("composition_guard")
    owner = getattr(guard, "_native_memory_authority_read_numeric", None)
    source = None
    if owner is not None:
        from src.memory.header_bounds import HeaderBoundsError
        if type(owner) is not tuple or len(owner) != 10:
            raise HeaderBoundsError("memory_report_source_resource_owner_changed")
        candidate_source = owner[0]
        if (type(candidate_source) is not _NativeMemoryDispatchSource
                or _MEMORY_SOURCES.get(id(candidate_source)) is not candidate_source):
            raise HeaderBoundsError("memory_report_source_resource_owner_changed")
        if candidate_source.admission.report_source is not None:
            source = _memory_report_source_for_revalidation(db, candidate_source.admission)
    if source is None:
        return await m5._consume_prepared_m5_text(db, value, prepared, header_budget=budget)
    collector, active_token, trace_token = _begin_memory_report_source_numeric_trace(
        db, source, budget, "vault-consume")
    try:
        # The original UTF/digest, bounded Secret inventory and structural
        # sanitization remain one unchanged original call. Retention follows
        # its actual digest comparison and successful sanitized result.
        result = await m5._consume_prepared_m5_text(db, value, prepared, header_budget=budget)
        retained = _finish_memory_report_source_numeric_trace(collector)
        from src.memory.header_bounds import _native_memory_ascii_string_width
        collector._storage_debit(3 + _native_memory_ascii_string_width("_source_vault_numeric_confirmed") + 4,
            "source-vault-success-slot")
        object.__setattr__(collector.stage, "_source_vault_numeric_confirmed", retained)
        return result
    finally:
        _end_memory_report_numeric_trace(collector, active_token, trace_token)


async def prepare_memory_admission(db, *, operator, method, request, idempotency_key,
                                   host_boot_nonce, composition_binding_digest, original_deadline,
                                   header_budget=None):
    """Authenticated ingress stages only data before its original admission writer."""
    from src.auth.service import AuthenticatedOperator
    from src.auth.ownership import _current_root
    from src.memory import m5
    from src.db.models import MemoryProposal, MemoryProposalStatus
    from dataclasses import asdict
    if (type(operator) is not AuthenticatedOperator or method not in MEMORY_METHODS
        or db.info.get("native_writer_started") or type(request) is not dict):
        raise NativeServiceBlocked("native_memory_original_ingress_required")
    from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION, MEMORY_DESCRIPTORS
    budget = header_budget if header_budget is not None else HeaderReadBudget()
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    connection = await db.connection()
    if not (await connection.get_raw_connection()).driver_connection.in_transaction:
        from sqlalchemy import text
        await db.execute(text("BEGIN"))
    await budget.certify(db, OPERATOR_SESSION, (operator.session_id,))
    await _current_root(db, operator)
    if (original_deadline.tzinfo != timezone.utc or not 0 <
        (original_deadline - datetime.now(timezone.utc)).total_seconds() <= 30):
        raise NativeServiceBlocked("native_memory_original_cutoff_invalid")
    principal, session = operator.principal.principal_id, operator.session_id
    common = {"schema_version": 1, "method": method, "operator_principal_id": principal,
        "operator_session_id": session, "opaque_ref": "native-memory:" + _digest(
            {"principal": principal, "session": session, "method": method, "key": idempotency_key}),
        "idempotency_key": idempotency_key, "original_deadline": original_deadline.isoformat(),
        "host_boot_nonce": host_boot_nonce, "composition_binding_digest": composition_binding_digest}
    if method == "memory.forget":
        if set(request) != {"record_ref", "mode", "privacy_boundary", "reason"}:
            raise NativeServiceBlocked("native_memory_candidate_invalid")
        reason = request["reason"]
        if reason is not None and (type(reason) is not str or len(reason) > 500):
            raise NativeServiceBlocked("native_memory_candidate_invalid")
        prepared = None if reason is None else asdict(await _prepare_original_memory_text(db, reason, budget))
        admission = NativeMemoryMutationAdmission.from_candidate({**common, **request, "prepared_reason": prepared})
    else:
        if method == "memory.propose":
            if set(request) != {"task_id", "expected_task_revision", "attempt_id"}:
                raise NativeServiceBlocked("native_memory_candidate_invalid")
            source, proof = await source_binding(db, principal_id=principal, session_id=session,
                task_id=request["task_id"], revision=request["expected_task_revision"], attempt_id=request["attempt_id"], header_budget=budget)
            if source["capability_id"] != REPORT_SOURCE_CAPABILITY:
                raise NativeServiceBlocked("native_memory_source_profile_unsupported")
            _native_memory_source_candidate_text_pay(budget, proof)
            admission = NativeMemoryMutationAdmission.from_candidate({**common, "source": source,
                "prepared_text": asdict(await _prepare_original_memory_text(db, m5._structured_source_candidate(proof), budget))})
        else:
            keys = {"proposal_id", "action", "expected_revision", "expected_preview_text_digest",
                "expected_task_revision", "expected_goal_revision", "edited_text", "decision_effect",
                "preferred_capability_id", "corrects_memory_id", "reason"}
            if set(request) != keys:
                raise NativeServiceBlocked("native_memory_candidate_invalid")
            await budget.certify(db, MEMORY_DESCRIPTORS["memory_proposals"], (request["proposal_id"],))
            proposal = await db.get(MemoryProposal, request["proposal_id"])
            if (proposal is None or proposal.owner_principal_id != principal or proposal.owner_session_id != session
                or proposal.schema_version != "memory_proposal.v1" or proposal.status != MemoryProposalStatus.proposed
                or proposal.revision != request["expected_revision"] or proposal.expires_at is None
                or proposal.goal_revision != request["expected_goal_revision"]
                or proposal.preview_text_digest != request["expected_preview_text_digest"]):
                raise NativeServiceBlocked("native_memory_original_review_changed")
            source, proof = await source_binding(db, principal_id=principal, session_id=session,
                task_id=proposal.source_task_id, revision=request["expected_task_revision"], attempt_id=proposal.source_attempt_id, header_budget=budget)
            if source["capability_id"] != REPORT_SOURCE_CAPABILITY:
                raise NativeServiceBlocked("native_memory_source_profile_unsupported")
            expires = m5._utc(proposal.expires_at)
            common["original_deadline"] = min(original_deadline, expires).isoformat()
            text = request["edited_text"] if request["edited_text"] is not None else proposal.preview_text or ""
            values = {key: item for key, item in request.items() if key not in {"expected_task_revision", "expected_goal_revision"}}
            admission = NativeMemoryMutationAdmission.from_candidate({**common, **values, "source": source,
                "proposal_schema": proposal.schema_version, "proposal_expires_at": expires.isoformat(),
                "prepared_text": asdict(await _prepare_original_memory_text(db, text, budget))})
    admission = replace(admission, header_budget=budget)
    admission = await stage_memory_report_source(db, admission,
        _report_input_stage=None if method == "memory.forget"
            else getattr(proof, "_native_memory_report_input_stage", None))
    await validate_original_memory_owner(db, admission)
    return admission


async def validate_original_memory_owner(db, admission, *, _numeric_source=None):
    """Metadata-only recheck in the original authority/effect writer."""
    from src.memory import m5
    from src.db.models import Memory, MemoryProposal, MemoryProposalStatus, MemoryTombstone
    from src.memory.repository import _canonical_memory_deletion_marker
    if type(admission) is not NativeMemoryMutationAdmission:
        raise NativeServiceBlocked("native_memory_original_candidate_required")
    from src.memory.header_bounds import HeaderReadBudget, SESSION, MEMORY_DESCRIPTORS
    budget = admission.header_budget
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    if _numeric_source is not None:
        _native_memory_report_source_capacity_checked(_numeric_source, db, budget)
    value = admission.candidate()
    session_trace = None
    session_collector = session_active_token = session_trace_token = None
    if _numeric_source is not None:
        if (_numeric_source.admission is not admission
                or _numeric_source.operator.session_id != value["operator_session_id"]
                or _numeric_source.operator.principal.principal_id != value["operator_principal_id"]):
            from src.memory.header_bounds import HeaderBoundsError
            raise HeaderBoundsError("memory_report_source_resource_owner_changed")
        session_collector, session_active_token, session_trace_token = _begin_memory_report_source_numeric_trace(
            db, _numeric_source, budget, "owner-session")
    try:
        session_certificate = await budget.certify(db, SESSION, (value["operator_session_id"],),
            _header_budget=budget if session_collector is not None else None)
        if session_collector is not None:
            _checked_memory_report_source_trace_certificate(session_collector, session_certificate, SESSION, value["operator_session_id"])
            session_trace = _finish_memory_report_source_numeric_trace(session_collector)
    finally:
        if session_collector is not None:
            _end_memory_report_numeric_trace(session_collector, session_active_token, session_trace_token)
    _native_memory_source_pay("authority", "session-model-delivery", session_certificate.upper_bytes)
    await m5._require_original_m5_session(db, value["operator_principal_id"], value["operator_session_id"])
    _native_memory_source_pay("authority", "deadline-current-time", 44)
    _native_memory_source_pay("authority", "deadline-parse", 44)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
        raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    if value["method"] != "memory.forget":
        old = value["source"]
        current, proof = await source_binding(db, principal_id=value["operator_principal_id"],
            session_id=value["operator_session_id"], task_id=old["task_id"],
            revision=old["expected_task_revision"], attempt_id=old["attempt_id"], header_budget=admission.header_budget,
            _report_source=admission.report_source, _numeric_source=_numeric_source)
        if current != old:
            raise NativeServiceBlocked("native_memory_original_source_changed")
        if proof.task.capability_id == REPORT_SOURCE_CAPABILITY:
            _native_memory_source_candidate_text_pay(budget, proof)
        original = m5._structured_source_candidate(proof)
        if value["method"] == "memory.applyReviewed":
            await budget.certify(db, MEMORY_DESCRIPTORS["memory_proposals"], (value["proposal_id"],))
            proposal = await db.get(MemoryProposal, value["proposal_id"])
            if (proposal is None or proposal.owner_principal_id != value["operator_principal_id"]
                or proposal.owner_session_id != value["operator_session_id"]
                or proposal.schema_version != value["proposal_schema"]
                or proposal.status != MemoryProposalStatus.proposed
                or proposal.revision != value["expected_revision"]
                or proposal.preview_text_digest != value["expected_preview_text_digest"]
                or proposal.expires_at is None
                or m5._utc(proposal.expires_at).isoformat() != value["proposal_expires_at"]
                or proposal.source_task_id != old["task_id"] or proposal.source_attempt_id != old["attempt_id"]):
                raise NativeServiceBlocked("native_memory_original_review_changed")
            original = value["edited_text"] if value["edited_text"] is not None else proposal.preview_text or ""
        _native_memory_source_prepared_text_model_pay(budget, value["prepared_text"])
        await _consume_original_memory_text(db, original, m5.PreparedM5Text(**value["prepared_text"]), budget)
    else:
        from src.memory.composition_headers import locate_exact_rows
        if not await locate_exact_rows(db, MEMORY_DESCRIPTORS["memories"], value["record_ref"], budget):
            raise NativeServiceBlocked("native_memory_original_record_changed")
        await budget.certify(db, MEMORY_DESCRIPTORS["memories"], (value["record_ref"],))
        record = await db.get(Memory, value["record_ref"])
        from src.memory.composition_headers import locate_tombstones
        tombstone_ids = await locate_tombstones(db, value["record_ref"], budget)
        if tombstone_ids:
            await budget.certify(db, MEMORY_DESCRIPTORS["memory_tombstones"], tombstone_ids)
        if (record is None or record.source_session_id != value["operator_session_id"]
            or _canonical_memory_deletion_marker(record) is not None
            or await db.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id == record.id)) is not None):
            raise NativeServiceBlocked("native_memory_original_record_changed")
        if value["prepared_reason"] is not None:
            _native_memory_source_prepared_text_model_pay(budget, value["prepared_reason"])
            await _consume_original_memory_text(db, value["reason"], m5.PreparedM5Text(**value["prepared_reason"]), budget)
    if _numeric_source is not None:
        _commit_memory_report_source_numeric_trace(_numeric_source, proof, session_trace)
    return value


@dataclass(frozen=True)
class MemoryOwnerEffect:
    """Actual owner result; caller must seal it before committing this writer."""
    method: str
    candidate_digest: str
    status: str
    value_json: str | None
    audit_event_id: str | None
    proposal_id: str | None
    record_id: str | None


def _native_memory_mutation_allocation_upper(method, widths, stage, *, audit=None):
    """Scalar sizing at the actual allocation site; no fee-vector copy."""
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if stage in ("prepared-text-kwargs", "prepared-text-construction") and method != "memory.forget":
        return widths["source_upper"]
    if stage == "audit-details-construction" and method == "memory.propose":
        return widths["details_upper"]
    if stage == "audit-event-kwargs" and method == "memory.propose":
        from src.db.models import AuditEvent
        if type(audit) is not AuditEvent:
            raise HeaderBoundsError("memory_mutation_numeric_audit_unavailable")
        # Eight original kwargs fields: arithmetic only. The actual effect
        # hook must not first allocate an unpaid numeric sizing dictionary.
        return (2 + 7
            + _native_memory_ascii_string_width("actor") + 1 + _native_memory_source_value_upper(audit.actor)
            + _native_memory_ascii_string_width("session_id") + 1 + _native_memory_source_value_upper(audit.session_id)
            + _native_memory_ascii_string_width("event_type") + 1 + _native_memory_source_value_upper(audit.event_type)
            + _native_memory_ascii_string_width("tool_name") + 1 + _native_memory_source_value_upper(audit.tool_name)
            + _native_memory_ascii_string_width("policy_mode") + 1 + _native_memory_source_value_upper(audit.policy_mode)
            + _native_memory_ascii_string_width("summary") + 1 + _native_memory_source_value_upper(audit.summary)
            + _native_memory_ascii_string_width("details") + 1 + widths["details_upper"]
            + _native_memory_ascii_string_width("_prepared_event") + 1 + widths["audit_upper"])
    if stage in ("result-value-construction", "result-canonical"):
        return widths["result_upper"]
    if stage in ("cutoff-now", "cutoff-parse"):
        return 44
    if stage in ("effect-construction", "effect-return"):
        return widths["effect_upper"]
    raise HeaderBoundsError("memory_mutation_numeric_appearance_unavailable")


def _native_memory_mutation_allocation_entries(method, widths, *, audit=None):
    """Only allocations owned by perform_memory_mutation, once per call.

    The sealed successful outcome supplies these numeric upper bounds. Source
    validation, the two authority callbacks, M5 application and subsequent
    retention/publication each have separate original owners and schedules.
    """
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_object_width
    if method not in {"memory.propose", "memory.applyReviewed", "memory.forget"}:
        raise HeaderBoundsError("memory_mutation_numeric_method_unavailable")
    for name in ("source_upper", "audit_upper", "details_upper", "result_upper", "effect_upper"):
        _native_memory_source_capacity(widths[name])
    entries = []
    def add(stage, upper):
        entries.append((("native-memory-mutation", stage), _native_memory_source_capacity(upper)))
    if method != "memory.forget":
        # kwargs expansion and the PreparedM5Text instance are distinct copies.
        add("prepared-text-kwargs", widths["source_upper"])
        add("prepared-text-construction", widths["source_upper"])
    if method == "memory.propose":
        add("audit-details-construction", widths["details_upper"])
        add("audit-event-kwargs", _native_memory_mutation_allocation_upper(
            method, widths, "audit-event-kwargs", audit=audit))
    add("result-value-construction", widths["result_upper"])
    add("cutoff-now", 44)
    add("cutoff-parse", 44)
    add("result-canonical", widths["result_upper"])
    add("effect-construction", widths["effect_upper"])
    add("effect-return", widths["effect_upper"])
    return tuple(entries)


def _native_memory_mutation_allocation_construction_numeric_upper(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE original mutation forecast constructor, no retained parent tuple."""
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    from src.memory.header_bounds import HeaderBoundsError
    method = prepared._method
    if method == "memory.propose":
        count = 10
    elif method == "memory.applyReviewed":
        count = 8
    elif method == "memory.forget":
        count = 6
    else:
        raise HeaderBoundsError("memory_mutation_numeric_method_unavailable")
    # Same five closed original capacities are validated without constructing
    # the helper's appearance list or future effect/result/PreparedM5Text.
    for name in ("source_upper", "audit_upper", "details_upper", "result_upper", "effect_upper"):
        _native_memory_source_capacity(retention_widths[name])
    stage = len("prepared-text-construction") + 2
    appearance = 3 + len("native-memory-mutation") + 2 + stage
    pair = 3 + appearance + 20
    triple = 4 + len("effect") + 2 + appearance + 20
    result = (2 + 2 + 3 * (len("memory.applyReviewed") + 3)
        + 2 * (2 + count - 1 + count * pair)
        + count * appearance + 2 + count - 1 + count * triple
        + 2 * (2 + 7 + 8 * 4))  # add closure and outer tuple generator
    if method == "memory.propose":
        from src.db.models import AuditEvent
        audit = prepared.audit_event
        if type(audit) is not AuditEvent:
            raise HeaderBoundsError("memory_mutation_numeric_audit_unavailable")
        # The original nested audit kwargs helper allocates exactly one eight
        # entry numerical map and one object-width generator. Its values are
        # encoded-width integers, not a copied audit body. Original six scalar
        # attributes must retain the source helper's closed-value domain.
        for name in ("actor", "session_id", "event_type", "tool_name", "policy_mode", "summary"):
            _native_memory_source_value_upper(getattr(audit, name))
        result += 2 + 7 + 8 * (6 * len("_prepared_event") + 3 + 20)
        result += 2 + 7 + 8 * 4
    return result


def original_native_memory_mutation_allocation_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths):
    """Effect allocation contribution from one registered, unconsumed plan.

    This is not a complete effect reservation: in particular it does not price
    the two genuine authority_check calls or either Source validation call.
    No candidate, future receipt, row or reference is constructed to size it.
    The prepared REPORT path passes its identical original read context; a
    reconstructed context cannot satisfy the original sealed apply identity.
    """
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError
    from src.memory.m5 import PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS, _NATIVE_MUTATION_PLAN_SEAL
    from src.workspace.accounting_witness import CompositionSessionGuard
    if (type(operation) is not _MemoryOwnerSourceOperation
            or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or type(prepared) is not PreparedNativeMemoryMutation
            or _NATIVE_MUTATION_PLANS.get(id(prepared)) is not prepared
            or prepared._seal is not _NATIVE_MUTATION_PLAN_SEAL
            or operation._prepared is not prepared or prepared.consumed
            or operation._effect is not None or prepared._db is not operation.db
            or prepared._transaction is not operation.transaction or prepared._driver is not operation.driver
            or prepared._candidate_digest != operation.source.admission.candidate_digest):
        raise HeaderBoundsError("memory_mutation_numeric_owner_changed")
    guard = operation.db.info.get("composition_guard")
    capacity = getattr(guard, "_native_memory_source_capacity_facts", None)
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    retained = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if (type(guard) is not CompositionSessionGuard
            or capacity is None or capacity[0] is not operation or capacity[1] is not prepared
            or capacity[2] is not certificate or capacity[3] is not facts
            or retained is None or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not retention_widths
            or owner is None or owner[0] is not operation or owner[1] is not prepared
            or owner[2] is not certificate or owner[3] is not asyncio.current_task()
            or owner[4] is not operation.transaction or owner[5] is not operation.driver
            or operation.transaction is not operation.db.sync_session.get_transaction()
            or not operation.driver.in_transaction
            or certificate.budget is not operation.source.admission.header_budget):
        raise HeaderBoundsError("memory_mutation_numeric_owner_changed")
    return tuple(("effect", appearance, upper) for appearance, upper in
        _native_memory_mutation_allocation_entries(prepared._method, retention_widths, audit=prepared.audit_event))


def _native_memory_dispatch_validation_numeric_owner(operation, prepared, *,
        certificate, facts, retention_widths):
    """Nonallocating original lineage gate, shared by planning owners."""
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError
    from src.memory.m5 import PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS, _NATIVE_MUTATION_PLAN_SEAL
    from src.workspace.accounting_witness import CompositionSessionGuard
    if (type(operation) is not _MemoryOwnerSourceOperation
            or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or type(prepared) is not PreparedNativeMemoryMutation
            or _NATIVE_MUTATION_PLANS.get(id(prepared)) is not prepared
            or prepared._seal is not _NATIVE_MUTATION_PLAN_SEAL
            or operation._prepared is not prepared or prepared.consumed
            or operation._effect is not None or prepared._db is not operation.db
            or prepared._transaction is not operation.transaction or prepared._driver is not operation.driver
            or prepared._candidate_digest != operation.source.admission.candidate_digest):
        raise HeaderBoundsError("memory_dispatch_validation_numeric_owner_changed")
    guard = operation.db.info.get("composition_guard")
    capacity = getattr(guard, "_native_memory_source_capacity_facts", None)
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    retained = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if (type(guard) is not CompositionSessionGuard
            or capacity is None or capacity[0] is not operation or capacity[1] is not prepared
            or capacity[2] is not certificate or capacity[3] is not facts
            or retained is None or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not retention_widths
            or owner is None or owner[0] is not operation or owner[1] is not prepared
            or owner[2] is not certificate or owner[3] is not asyncio.current_task()
            or owner[4] is not operation.transaction or owner[5] is not operation.driver
            or operation.transaction is not operation.db.sync_session.get_transaction()
            or not operation.driver.in_transaction
            or certificate.budget is not operation.source.admission.header_budget
            or guard.header_budget is not certificate.budget
            or _native_memory_source_numeric_frame() is not certificate.budget):
        raise HeaderBoundsError("memory_dispatch_validation_numeric_owner_changed")


def _original_native_memory_source_schedule_planning_upper(*, result_present):
    """Storage for ONE candidate9 plus context108/120 forecast construction.

    Capacity arithmetic only: no vector, payload, certificate or owner is
    constructed. Widths below size the helper's metadata, not future Source
    bodies. Integers use their original signed-int64 decimal ceiling20. Each
    generator roster, result container, entry pair and newly built label is
    counted separately; a container includes its children again.
    """
    from src.memory.header_bounds import HeaderBoundsError
    if type(result_present) is not bool:
        raise HeaderBoundsError("memory_dispatch_planning_shape_unavailable")
    # Exact closed stage grammar from _original_native_binding_numeric_entries:
    # 25 plain names, 7 constructor tuples, 28 manifest tuples =60 entries.
    nested = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name = max(nested, len("required-plus-goals-inference-set") + 2)
    binding_label = 5 + len("composition-binding-parser") + 2 + 4 + name
    binding_pair = 3 + binding_label + 20
    input_pair = 3 + name + 20
    # Original60: labels/pairs,35 nested names,append list and return tuple;
    # 23 initial input pairs,2x7 manifest input pairs,6 sizing input pairs;
    # original numeric generators14 domains,34 methods,6 manifest fields.
    binding = (60 * (binding_label + binding_pair) + 35 * nested
        + 2 + 2 * (2 + 60 * binding_pair + 59)
        + 23 * input_pair + (2 + 23 * input_pair + 22)
        + 2 * (7 * input_pair + (2 + 7 * input_pair + 6))
        + 6 * input_pair + (2 + 6 * input_pair + 5)
        + (2 + 14 * 20 + 13) + (2 + 34 * 20 + 33)
        + (2 + 6 * 20 + 5))
    source_label = (4 + len("native-memory-source") + 2
        + len("context-binding-parser") + 2 + name)
    pair = 3 + source_label + 20
    c2 = 2 + 2 * pair + 1
    c3 = 2 + 3 * pair + 2
    c4 = 2 + 4 * pair + 3
    c5 = 2 + 5 * pair + 4
    c7 = 2 + 7 * pair + 6
    c9 = 2 + 9 * pair + 8
    # Validation4 and the three-stage generators each produce their own tuple.
    validation = 4 * (source_label + pair) + 2 * c4
    candidate = (validation + 3 * (source_label + pair) + 2 * c3
        + 2 * (source_label + pair) + 2 * c9)
    # Admission's4-field sizing dict + its items and numeric generator, then
    # 4+3 concatenation7 and +2 concatenation9. No future candidate is made.
    admission_fields = (len("candidate_json") + len("candidate_digest")
        + len("report_source") + len("header_budget") + 4 * (2 + 1 + 20))
    admission_map = 2 + admission_fields + 3
    admission_items = admission_fields + 4 * 3
    admission = (validation + 3 * (source_label + pair) + 2 * c3
        + 2 * (source_label + pair) + c2 + c7 + c9
        + admission_map + admission_items + (2 + 4 * 20 + 3))
    # Parser remap60; digest first builds a second original60, filters21, then
    # remaps21. Its7-name set is a separate container. Preserve all repeats.
    parser = binding + 60 * (source_label + pair) + 2 * (2 + 60 * pair + 59)
    digest_names = (len("payload-methods-list") + len("payload-dependencies-list")
        + len("binding-payload") + len("binding-canonical")
        + len("binding-encoded") + len("binding-digest") + len("dependency-payloads"))
    digest = (binding + 2 + digest_names + 7 * 2 + 6
        + 21 * binding_pair + 2 * (2 + 21 * binding_pair + 20)
        + 21 * (source_label + pair) + 2 * (2 + 21 * pair + 20))
    context = ((2 + 6 * 20 + 5) + 6 * (source_label + pair) + 2 * c5
        + admission + candidate + parser + digest
        + 3 * (source_label + pair) + 2 * c3
        + 2 * (2 + 108 * pair + 107))
    if result_present:
        #12 additional labels/pairs,one2-stage generator and its tuple,
        # RHS12 starred list/tuple and the concatenated final120 tuple.
        context += (12 * (source_label + pair) + 2 * c2
            + 2 * (2 + 12 * pair + 11) + (2 + 120 * pair + 119))
    return candidate + context


def _original_native_memory_dispatch_planning_upper(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE original185 forecast constructor's storage, never validator fees.

    The factory prepays this scalar before building R445 and retains the SAME
    vector. Real metadata69/read-helper Counters have their separate owner.
    This function cannot grant Source, a body read, or reservation capacity.
    """
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    from src.memory.header_bounds import _native_memory_ascii_string_width
    nested = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name = max(nested, len("required-plus-goals-inference-set") + 2)
    binding_label = 5 + len("composition-binding-parser") + 2 + 4 + name
    binding_pair = 3 + binding_label + 20
    input_pair = 3 + name + 20
    binding = (60 * (binding_label + binding_pair) + 35 * nested
        + 2 + 2 * (2 + 60 * binding_pair + 59)
        + 23 * input_pair + (2 + 23 * input_pair + 22)
        + 2 * (7 * input_pair + (2 + 7 * input_pair + 6))
        + 6 * input_pair + (2 + 6 * input_pair + 5)
        + (2 + 14 * 20 + 13) + (2 + 34 * 20 + 33)
        + (2 + 6 * 20 + 5))
    # Inner to_json filter28,2x payload4,successive concats32/36/final41.
    # Payload labels include the actual occurrence0/1 digit; upper20 preserves
    # the original metadata integer ceiling without constructing that label.
    payload_label = 4 + len("payload") + 2 + 20 + name
    label = (4 + len("native-memory-source") + 2
        + len("dispatch-binding-json") + 2 + max(name, payload_label))
    pair = 3 + label + 20
    inner_pair = 3 + max(name, payload_label) + 20
    names_set = (2 + 3 + 4 * 2 + len("payload-methods-list")
        + len("payload-dependencies-list") + len("binding-payload") + len("dependency-payloads"))
    binding_json = (binding + names_set
        + 28 * inner_pair + 2 * (2 + 28 * inner_pair + 27)
        + 8 * (payload_label + inner_pair) + 4 * (2 + 4 * inner_pair + 3)
        + (2 + 32 * inner_pair + 31) + (2 + 36 * inner_pair + 35)
        + 5 * inner_pair + (2 + 5 * inner_pair + 4)
        + (2 + 41 * inner_pair + 40)
        + 41 * (label + pair) + 2 * (2 + 41 * pair + 40))
    witness = _native_memory_dispatch_witness_upper(operation.scope.witness)
    # Existing20-field paid witness items + numeric generator; its one methods
    # tuple has the original34 ceiling. This is Source data sizing, no copy.
    witness_sizing = 2 * witness + (2 + 20 * 20 + 19) + (2 + 34 * 20 + 33)
    witness_inner = 3 + len("witness-digest-canonical") + 2 + 20
    witness_entries = (11 * witness_inner + (2 + 11 * witness_inner + 10)
        + 11 * witness_inner + 2 * (2 + 11 * witness_inner + 10)
        + 11 * (label + pair) + 2 * (2 + 11 * pair + 10))
    copies_dates = (4 * input_pair + (2 + 4 * input_pair + 3)
        + 4 * (label + pair) + 2 * (2 + 4 * pair + 3))
    # Locator sizing creates4 input pairs/their tuple and one numeric generator.
    locator_pair = 3 + len("original_checkpoint_digest") + 2 + 20
    locator = 4 * locator_pair + (2 + 4 * locator_pair + 3) + (2 + 4 * 20 + 3)
    # Existing address tuple is built to select paid scalar dimensions.
    address = 3 + len("workflow_run_states") + 2 + _native_memory_ascii_string_width(operation.run.run_identity)
    # Exact successive R445 entry rosters15,56,65,185 after the initial11;
    # every concat is retained here in addition to its already-priced input.
    concatenations = ((2 + 15 * pair + 14) + (2 + 56 * pair + 55)
        + (2 + 65 * pair + 64) + (2 + 185 * pair + 184))
    triple = 4 + len("effect") + 2 + label + 20
    final = 185 * triple + 2 * (2 + 185 * triple + 184)
    return (witness_sizing + witness_entries + copies_dates + binding_json
        + _original_native_memory_source_schedule_planning_upper(result_present=True)
        + locator + address + concatenations + final)


def original_native_memory_dispatch_validation_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths, result_present):
    """ONE Source-validator call's allocations; DB-owner trace is separate.

    Conservatively includes the result-present branch regardless of the bool
    compatibility argument; unused reservations stay spent. Includes its one
    Admission.candidate and one memory_context schedule. The
    composer must not append those nested R425 schedules a second time. The
    original OPERATOR_SESSION certify/currentRoot/inventory validation still
    execute once each and require their genuine guarded numeric trace.
    """
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    from src.memory.header_bounds import WRS_BY_RUN
    if type(result_present) is not bool:
        raise HeaderBoundsError("memory_dispatch_validation_numeric_shape_unavailable")
    address = (WRS_BY_RUN.table, operation.run.run_identity)
    text = facts["text_bytes"][address]
    journal_bytes = max(2, text.get("checkpoint_receipts_json", 2),
        retention_widths["future_text_bytes"]["checkpoint_receipts_json"],
        retention_widths["completion_future_text_bytes"]["checkpoint_receipts_json"])
    witness_upper = _native_memory_dispatch_witness_upper(operation.scope.witness)
    entries = tuple((("native-memory-source", "dispatch-validation", stage), upper)
        for stage, upper in _native_memory_dispatch_witness_entries(journal_bytes, witness_upper))
    entries += tuple((("native-memory-source", "dispatch-validation", stage), upper)
        for stage, upper in (("scope-witness-copy", witness_upper),
            ("deadline-replace",44),("deadline-astimezone",44),("current-now",44)))
    entries += tuple((("native-memory-source", "dispatch-binding-json", stage), upper)
        for stage, upper in _native_memory_dispatch_binding_json_entries(retention_widths["binding_text_bytes"]))
    entries += original_native_memory_source_candidate_numeric_entries(candidate_upper=retention_widths["source_upper"])
    from src.memory.header_bounds import _native_memory_ascii_string_width
    from src.workspace.accounting_witness import MEMORY_REFERENCE_PROFILE, MEMORY_ORIGINAL_CHECKPOINT
    locator_upper = 2 + 3 + sum(_native_memory_ascii_string_width(name) + 1 + upper
        for name, upper in (("profile", _native_memory_ascii_string_width(MEMORY_REFERENCE_PROFILE)),
            ("invocation_ref", _native_memory_ascii_string_width(operation.run.run_identity)),
            ("original_checkpoint_id", _native_memory_ascii_string_width(MEMORY_ORIGINAL_CHECKPOINT)),
            ("original_checkpoint_digest",66)))
    entries += original_native_memory_source_context_numeric_entries(
        context_upper=retention_widths["context_upper"], candidate_upper=retention_widths["source_upper"],
        binding_text_bytes=retention_widths["binding_text_bytes"], result_upper=retention_widths["result_upper"],
        effect_upper=retention_widths["effect_upper"], locator_upper=locator_upper,
        result_present=True)
    return tuple(("effect", appearance, upper) for appearance, upper in entries)


def _native_memory_dispatch_read_label_roster(session_id):
    """Exactly the two original PK certification paths: 15 + 54 debits.

    Neither owner uses a unique-index locator: operator_sessions.id and
    runtime_composition_states.runtime_domain are their original sole PKs.
    Keep every repeated state/schema/row appearance, including duplicate labels.
    """
    from .ownership import DOMAINS
    from src.memory.header_bounds import OPERATOR_SESSION, RUNTIME_COMPOSITION
    for descriptor, identities in ((OPERATOR_SESSION, (session_id,)),
            (RUNTIME_COMPOSITION, tuple(DOMAINS))):
        yield ("exact-metadata", "initial-state")
        yield ("metadata-prequery", "descriptor-compiler", descriptor.table)
        yield ("metadata-prequery", "descriptor-xinfo", "delivery")
        yield ("metadata-prequery", "descriptor-table", "delivery")
        yield ("exact-metadata", "sqlite-version")
        yield ("exact-metadata", "encoding")
        for stage in ("delivery", "row-copy", "encoded-copy"):
            yield ("exact-metadata", "schema", descriptor.table, stage)
        for identity in identities:
            for stage in ("delivery", "row-copy", "encoded-copy"):
                yield ("exact-metadata", "row-header", descriptor.table, identity, stage)
        yield ("exact-metadata", "final-state")
        yield ("exact-header", descriptor.table, descriptor.key, identities)
        yield ("exact-metadata", "validation-state")


async def original_native_memory_dispatch_validation_read_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE genuine read-only validator call; forecast reuse never refunds it.

    Closed allocation labels belong to the separate conservative R445 vector.
    Only those exact labels are removed; the 69 original PK/header/state
    occurrences are retained separately with their actual numerical amounts.
    Unexpected labels or repetitions STOP instead of extending the grant.
    """
    import asyncio
    from collections import Counter
    from src.memory.header_bounds import HeaderBoundsError, _trace_memory_numeric_charges
    from src.memory.composition_headers import _common_trace_label_width
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    budget = certificate.budget
    if _native_memory_source_numeric_frame() is not budget:
        raise HeaderBoundsError("memory_dispatch_read_numeric_owner_changed")
    retained = getattr(operation.db.info["composition_guard"],
        "_native_memory_dispatch_allocation_numeric", None)
    if (type(retained) is not tuple or len(retained) != 6
            or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not facts
            or retained[4] is not retention_widths or type(retained[5]) is not tuple
            or len(retained[5]) != 185):
        raise HeaderBoundsError("memory_dispatch_read_allocation_lineage_unavailable")
    # Reuse the factory's ONE already-paid vector, never rebuild R445 just to
    # plan this trace. Factory constructor/intermediate prepayments are separate.
    # Source count185: witness11 + copies/dates4 + bindingJSON41 + candidate9
    # + context120 (6+admission9+candidate9+binding60+digest21+dates3+result12).
    allocations = retained[5]
    # Before any roster generator/Counter:69 fixed labels fit256; four session
    # identity appearances at512UTF8 and one14-domain header, max32ASCII/domain.
    metadata_storage_upper = 69 * 256 + 4 * (6 * 512 + 2) + 14 * 194
    budget.debit(4 * metadata_storage_upper,
        appearance=("memory-dispatch-read-trace", "planning-before-allocation"))
    budget.debit((69 + 185) * (256 + 20 + 3),
        appearance=("memory-dispatch-read-trace", "multiplicity-maps"))
    # A separate prepaid map retains EACH authentic forecast amount. Its
    # per-label lists are conservative multisets, never a count-only waiver.
    paid_storage_upper = 2
    for phase, label, amount in allocations:
        if phase != "effect" or type(amount) is not int or not 0 <= amount < 2**63:
            raise HeaderBoundsError("memory_dispatch_read_allocation_lineage_unavailable")
        paid_storage_upper += _common_trace_label_width(label) + 1 + 3 * (2 + 20 + 1)
    budget.debit(paid_storage_upper,
        appearance=("memory-dispatch-read-trace", "paid-amount-multisets"))
    # These are capacity-only labels from the fixed source grammar. Price each
    # temporary planning pair and each trace pair/list slot at int64 width20.
    # No body is serialized or fetched to obtain this storage upper.
    metadata_pair_upper = sum(3 + _common_trace_label_width(label) + 20
        for label in _native_memory_dispatch_read_label_roster(operation.source.operator.session_id))
    allocation_pair_upper = sum(3 + _common_trace_label_width(label) + 20
        for phase, label, amount in allocations)
    pair_upper = metadata_pair_upper + allocation_pair_upper
    count = 69 + len(allocations)
    vector_upper = 2 + pair_upper + max(0, count - 1)
    for stage, upper in (("planning-roster", 4 * vector_upper),
            ("trace-pairs", pair_upper), ("trace-list", vector_upper),
            ("selected-pairs", metadata_pair_upper),
            ("selected-list", vector_upper), ("retained-tuple", vector_upper),
            ("owner-lineage", 2 + vector_upper + 8 * 20 + 8)):
        budget.debit(upper, appearance=("memory-dispatch-read-trace", stage))
    expected = Counter(_native_memory_dispatch_read_label_roster(operation.source.operator.session_id))
    allocation_remaining = {}
    for phase, label, amount in allocations:
        if label not in allocation_remaining:
            allocation_remaining[label] = []
        allocation_remaining[label].append(amount)
    with _trace_memory_numeric_charges(budget) as trace:
        checked = await _validate_memory_dispatch_source(operation.db, operation.run, operation.scope)
    if (checked is not operation.source or asyncio.current_task() is not
            operation.db.info["composition_guard"]._native_memory_source_capacity_owner[3]
            or operation.transaction is not operation.db.sync_session.get_transaction()
            or not operation.driver.in_transaction or len(trace) > count):
        raise HeaderBoundsError("memory_dispatch_read_numeric_owner_changed")
    selected = []
    for appearance, upper in trace:
        if appearance in allocation_remaining:
            amounts = allocation_remaining[appearance]
            if (not amounts or type(upper) is not int
                    or not 0 <= upper < 2**63):
                raise HeaderBoundsError("memory_dispatch_read_numeric_roster_changed")
            # Consume the smallest sufficient slot. Unused conservative
            # occurrences stay paid; a larger observed debit cannot disappear.
            best_index = -1
            index = 0
            for paid_upper in amounts:
                if paid_upper >= upper and (best_index < 0 or paid_upper < amounts[best_index]):
                    best_index = index
                index += 1
            if best_index < 0:
                raise HeaderBoundsError("memory_dispatch_read_numeric_paid_amount_exceeded")
            amounts.pop(best_index)
            continue
        if (expected[appearance] <= 0 or type(upper) is not int or not 0 <= upper < 2**63):
            raise HeaderBoundsError("memory_dispatch_read_numeric_roster_changed")
        expected[appearance] -= 1
        selected.append((appearance, upper))
    if any(expected.values()) or len(selected) != 69:
        raise HeaderBoundsError("memory_dispatch_read_numeric_roster_changed")
    entries = tuple(selected)
    operation.db.info["composition_guard"]._native_memory_dispatch_read_numeric = (
        operation, prepared, certificate, facts, budget, asyncio.current_task(),
        operation.transaction, operation.driver, entries)
    return entries


def _native_memory_mutation_allocation_pay(db, operation, prepared, stage):
    """Pay the current original effect ledger before its owned allocation."""
    from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, _NativeMemoryNumericLedger, HeaderBoundsError
    active = _MEMORY_LEDGER_PHASE.get()
    if active is None:
        return
    ledger, phase = active
    from src.workspace.accounting_witness import _NativeMemoryRetention, _native_memory_capacity_slots
    if type(ledger) is not _NativeMemoryNumericLedger or phase != "effect":
        raise HeaderBoundsError("memory_mutation_numeric_owner_changed")
    _native_memory_capacity_slots(ledger)
    if operation is not ledger.operation or prepared is not ledger.prepared:
        raise HeaderBoundsError("memory_mutation_numeric_owner_changed")
    retention = getattr(operation.source, "_retention", None)
    binding = ledger.bindings[phase]
    lineage = getattr(retention, "_publication_numeric_facts", None)
    if (type(retention) is not _NativeMemoryRetention or retention.ledger is not ledger
            or ledger.operation is not operation or ledger.prepared is not prepared
            or retention.operation is not operation or retention.prepared is not prepared
            or binding[0] is not db or lineage is None or lineage[0] is not operation
            or lineage[1] is not prepared):
        raise HeaderBoundsError("memory_mutation_numeric_owner_changed")
    appearance = ("native-memory-mutation", stage)
    upper = _native_memory_mutation_allocation_upper(prepared._method, lineage[3], stage,
        audit=prepared.audit_event)
    ledger.budget.debit(upper, appearance=appearance)


async def perform_memory_mutation(db, admission, *, authority_check, source_operation=None,
                                  native_prepared_mutation=None, native_memory_report_source=None):
    """No nested writer, Vault key/decrypt, inference, or public success adoption."""
    from src.memory import m5
    from src.memory.control import _forget_memory_in_session
    from src.audit.repository import audit_repository
    if not db.in_transaction() or not db.info.get("native_writer_started"):
        raise NativeServiceBlocked("native_memory_original_writer_required")
    if source_operation is not None:
        source = await _validate_memory_owner_source(db, source_operation.run, source_operation)
        if (source.admission is not admission or source_operation._effect is not None):
            raise NativeServiceBlocked("native_memory_actual_candidate_unavailable")
    if native_prepared_mutation is not None:
        if source_operation is None or source_operation._prepared is not native_prepared_mutation:
            raise NativeServiceBlocked("native_memory_actual_preparation_unavailable")
        from src.workspace.accounting_witness import _NativeMemoryRetention
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
        retention = getattr(source_operation.source, "_retention", None)
        active = _MEMORY_LEDGER_PHASE.get()
        if (type(retention) is not _NativeMemoryRetention or active is None
                or active[0] is not retention.ledger or active[1] != "effect"
                or retention.operation is not source_operation
                or retention.prepared is not native_prepared_mutation):
            raise NativeServiceBlocked("native_memory_complete_forecast_required")
        retention._checked()
        await m5.validate_native_memory_mutation_plan(native_prepared_mutation, db=db,
            candidate_digest=admission.candidate_digest)
    await authority_check(db)
    value = await validate_original_memory_owner(db, admission,
        _numeric_source=source if source_operation is not None and admission.report_source is not None else None)
    method = value["method"]
    native_report_source = native_memory_report_source
    if value.get("source", {}).get("capability_id") == REPORT_SOURCE_CAPABILITY:
        if (type(admission.report_source) is not NativeMemoryReportSourceWitness
            or admission.report_source.candidate_digest != admission.candidate_digest):
            raise NativeServiceBlocked("native_memory_original_report_witness_required")
        if native_report_source is None:
            _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation,
                "report-context-construction")
            native_report_source = _MemoryReportReadContext(admission.report_source, admission, db, authority_check)
        elif (type(native_report_source) is not _MemoryReportReadContext
              or native_report_source.source is not admission.report_source
              or native_report_source.admission is not admission or native_report_source.writer is not db):
            raise NativeServiceBlocked("native_memory_actual_report_context_unavailable")
    elif native_report_source is not None:
        raise NativeServiceBlocked("native_memory_actual_report_context_unavailable")
    principal, session = value["operator_principal_id"], value["operator_session_id"]
    proposal_id = record_id = audit_id = None
    result_value = None
    if method == "memory.propose":
        source = value["source"]
        for stage in ("prepared-text-kwargs", "prepared-text-construction"):
            _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation, stage)
        prepared_text = m5.PreparedM5Text(**value["prepared_text"])
        result = await m5._create_memory_proposal_in_session(db, owner_principal_id=principal,
            owner_session_id=session, task_id=source["task_id"],
            expected_task_revision=source["expected_task_revision"], attempt_id=source["attempt_id"],
            prepared_text=prepared_text, require_original_session=True,
            native_memory_report_source=native_report_source,
            native_prepared_mutation=native_prepared_mutation)
        proposal_id = result["proposal_id"]
        for stage in ("audit-details-construction", "audit-event-kwargs"):
            _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation, stage)
        event = await audit_repository._log_event_in_session(db, actor=principal, session_id=session,
            event_type="memory_learning_proposed", tool_name="memory_control", policy_mode="operator_controlled",
            summary="Operator requested verified work-board memory",
            details={"proposal_id": proposal_id, "source_task_id": source["task_id"],
                "source_attempt_id": source["attempt_id"], "status": result["status"]},
            **({"_prepared_event": native_prepared_mutation.audit_event}
               if native_prepared_mutation is not None else {}))
        audit_id = event.id
        if result["status"] == "proposed":
            _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation,
                "result-value-construction")
            result_value = {"proposal_ref": proposal_id, "revision": result["revision"], "state": "proposed"}
    elif method == "memory.applyReviewed":
        source = value["source"]
        for stage in ("prepared-text-kwargs", "prepared-text-construction"):
            _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation, stage)
        prepared_text = m5.PreparedM5Text(**value["prepared_text"])
        result = await m5._apply_memory_proposal_action_in_session(db, owner_principal_id=principal,
            owner_session_id=session, proposal_id=value["proposal_id"], action=value["action"],
            expected_revision=value["expected_revision"], expected_preview_text_digest=value["expected_preview_text_digest"],
            expected_task_revision=source["expected_task_revision"], expected_goal_revision=source["goal_revision"],
            edited_text=value["edited_text"], decision_effect=value["decision_effect"], reason=value["reason"],
            corrects_memory_id=value["corrects_memory_id"], preferred_capability_id=value["preferred_capability_id"],
            prepared_text=prepared_text, require_original_session=True,
            native_memory_report_source=native_report_source,
            native_prepared_mutation=native_prepared_mutation)
        proposal_id = result["proposal_id"]
        audit_id = result.get("audit_event_id")
        if result["status"] == "accepted" and audit_id is not None and not result.get("idempotent_replay"):
            record_id = result["accepted_memory_id"]
            _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation,
                "result-value-construction")
            result_value = {"record_ref": record_id}
    else:
        # The genuine sealed preparation already consumed/checked this exact
        # reason with the original Vault/read budget. Its original owner apply
        # branch ignores this argument and uses the same prepared audit/update.
        reason = None if native_prepared_mutation is not None or value["prepared_reason"] is None else await _consume_original_memory_text(
            db, value["reason"], m5.PreparedM5Text(**value["prepared_reason"]), admission.header_budget)
        result = await _forget_memory_in_session(db, owner_session_id=session, actor=principal,
            memory_id=value["record_ref"], mode=value["mode"], reason=reason,
            privacy_boundary=value["privacy_boundary"],
            native_memory_report_source=native_report_source,
            native_prepared_mutation=native_prepared_mutation)
        record_id = result["memory"]["id"]
        audit_id = result["audit_event_id"]
        _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation,
            "result-value-construction")
        result_value = {"record_ref": record_id}
    # Current original authority/cutoff wins even after the owner effect. Caller
    # commits only together with its protected operation result/retention seal.
    if native_prepared_mutation is not None and retention.future_slots:
        from src.workspace.accounting_witness import _bind_native_memory_future_integer_slots
        await _bind_native_memory_future_integer_slots(db, source_operation, native_prepared_mutation)
    await authority_check(db)
    if native_prepared_mutation is not None:
        await m5.validate_native_memory_mutation_plan(native_prepared_mutation, db=db,
            candidate_digest=admission.candidate_digest, consumed=True)
    for stage in ("cutoff-now", "cutoff-parse"):
        _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation, stage)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
        raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    if result_value is not None:
        _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation, "result-canonical")
        value_json = _canonical(result_value)
    else:
        value_json = None
    _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation, "effect-construction")
    effect = MemoryOwnerEffect(method, admission.candidate_digest,
        "succeeded" if result_value is not None else "blocked",
        value_json, audit_id, proposal_id, record_id)
    if source_operation is not None:
        await _validate_memory_owner_source(db, source_operation.run, source_operation)
        object.__setattr__(source_operation, "_effect", effect)
    _native_memory_mutation_allocation_pay(db, source_operation, native_prepared_mutation, "effect-return")
    return effect


def candidate_context(admission):
    if type(admission) is not NativeMemoryMutationAdmission:
        raise NativeServiceBlocked("native_memory_original_candidate_required")
    return _canonical({"schema_version": 1, "context_tag": MEMORY_CONTEXT_TAG,
        "candidate": admission.candidate(), "candidate_digest": admission.candidate_digest, "result": None})


def memory_context(run):
    from .ownership import RuntimeCompositionBinding
    try:
        from src.memory.header_bounds import strict_json_loads
        frame = _native_memory_source_numeric_frame()
        if frame is not None:
            from src.memory.header_bounds import _native_memory_utf8_width
            context_bytes = _native_memory_utf8_width(run.checkpoint_context_json)
            _native_memory_source_pay("context", "size-utf8", context_bytes)
            for stage in ("json-parser", "duplicate-pairs", "duplicate-dicts"):
                _native_memory_source_pay("context", stage, 6 * context_bytes + 2)
        context = strict_json_loads(run.checkpoint_context_json)
        if (type(context) is not dict or _native_memory_source_field_set(context, "context", "field-set") != {"schema_version", "context_tag", "candidate", "candidate_digest", "result"}
            or type(context["schema_version"]) is not int or context["schema_version"] != 1
            or context["context_tag"] != MEMORY_CONTEXT_TAG):
            raise ValueError("context")
        admission = NativeMemoryMutationAdmission.from_candidate(context["candidate"])
        candidate = admission.candidate()
        if frame is not None:
            for appearance, upper in _native_memory_source_binding_entries(
                    _native_memory_utf8_width(run.composition_binding_json)):
                frame.debit(upper, appearance=appearance)
        binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        if (run.job_kind != MEMORY_JOB_KIND or run.capability_version != "1" or run.max_attempts != 1
            or run.owner_kind != "user" or run.parent_job_id is not None or run.source_task_id is not None
            or run.owner_principal_id != candidate["operator_principal_id"]
            or run.operator_session_id != candidate["operator_session_id"] or run.session_id != run.operator_session_id
            or run.conversation_id != run.operator_session_id
            or context["candidate_digest"] != admission.candidate_digest or run.input_digest != admission.candidate_digest
            or binding.origin_method != candidate["method"] or binding.native_branch != "base"
            or _native_memory_source_binding_digest(binding, run.composition_binding_json) != candidate["composition_binding_digest"]
            or _native_memory_source_deadline_iso(run.deadline_at) != candidate["original_deadline"]
            or (run.goal_id, run.goal_revision) != ((candidate["source"]["goal_id"], candidate["source"]["goal_revision"])
                if "source" in candidate else (None, None))):
            raise ValueError("binding")
        result = context["result"]
        if result is not None:
            from .contracts import validate_result
            if (type(result) is not dict or _native_memory_source_field_set(result, "context", "result-field-set") != {"invocation_ref", "claim_ref", "candidate_digest", "result_digest", "payload", "effect", "retention"}
                or result["invocation_ref"] != run.run_identity or result["candidate_digest"] != admission.candidate_digest
                or result["result_digest"] != _native_memory_source_digest(result["payload"], "context", "result-digest")):
                raise ValueError("result")
            if frame is not None:
                result_upper = _native_memory_source_value_upper(result["payload"])
                _native_memory_source_pay("context", "result-validation", result_upper)
                _native_memory_source_pay("context", "result-validation-fields", result_upper)
                if result["payload"].get("status") != "blocked":
                    _native_memory_source_pay("context", "result-validation-value-fields", result_upper)
            validate_result(candidate["method"], result["payload"])
            if frame is not None:
                effect_upper = _native_memory_source_value_upper(result["effect"])
                _native_memory_source_pay("context", "effect-keywords", effect_upper)
                _native_memory_source_pay("context", "effect-construction", effect_upper)
            effect = MemoryOwnerEffect(**result["effect"])
            if effect.method != candidate["method"] or effect.candidate_digest != admission.candidate_digest:
                raise ValueError("effect")
            validate_memory_retention_locator(result["retention"], run.run_identity)
        return context
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NativeServiceBlocked("native_memory_private_context_changed") from exc


def validate_memory_retention_locator(locator, invocation_ref):
    """Closed durable address only; resolving it never recreates a live Source."""
    from src.workspace.accounting_witness import MEMORY_REFERENCE_PROFILE, MEMORY_ORIGINAL_CHECKPOINT
    if (type(locator) is not dict
        or _native_memory_source_field_set(locator, "retention-locator", "field-set") != {"profile", "invocation_ref", "original_checkpoint_id", "original_checkpoint_digest"}
        or locator["profile"] != MEMORY_REFERENCE_PROFILE
        or locator["invocation_ref"] != invocation_ref
        or locator["original_checkpoint_id"] != MEMORY_ORIGINAL_CHECKPOINT
        or type(locator["original_checkpoint_digest"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", locator["original_checkpoint_digest"]) is None):
        raise ValueError("native_memory_retention_locator_invalid")
    return locator


def _utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


async def validate_memory_spec(db, spec, admission):
    from src.auth.service import authenticate_principal
    from .ownership import method_dependencies
    candidate = await validate_original_memory_owner(db, admission)
    await certify_original_memory_principal(db, candidate["operator_principal_id"], admission.header_budget)
    principal = await authenticate_principal(candidate["operator_principal_id"], db=db)
    binding = spec.composition_binding
    goals = (candidate["source"]["goal_id"], candidate["source"]["goal_revision"]) if "source" in candidate else (None, None)
    if (spec.inputs != candidate or spec.identity.job_kind != MEMORY_JOB_KIND or spec.identity.capability_version != "1"
        or spec.identity.owner_kind != "user" or spec.identity.owner_principal_id != candidate["operator_principal_id"]
        or spec.session_id != candidate["operator_session_id"] or spec.operator_session_id != spec.session_id
        or spec.conversation_id != spec.session_id or spec.max_attempts != 1
        or spec.parent_job_id is not None or spec.source_task_id is not None
        or (spec.goal_id, spec.goal_revision) != goals or spec.dependencies or spec.resource_claims
        or binding is None or binding.origin_method != candidate["method"] or binding.native_branch != "base"
        or binding.binding_digest != candidate["composition_binding_digest"] or binding.host_package_digest is None
        or tuple(item.runtime_domain for item in binding.dependency_vector) != method_dependencies(candidate["method"], goal_bound=goals[0] is not None)
        or _utc(spec.deadline_at).isoformat() != candidate["original_deadline"]
        or _utc(spec.deadline_at) > datetime.now(timezone.utc) + timedelta(seconds=30)
        or not db.info.get("native_writer_started") or db.info.get("composition_writer_owner") != "durable_jobs"
        or spec.declared_authority != {"principal": candidate["operator_principal_id"], "owner_kind": "user",
            "session_id": candidate["operator_session_id"],
            "grants": sorted(str(getattr(grant, "value", grant)) for grant in principal.principal.grants)}):
        raise NativeServiceBlocked("native_memory_original_spec_changed")


async def native_memory_spec(db, *, admission, operator, binding):
    from src.auth.ownership import _current_root
    from src.auth.service import authenticate_principal
    from src.workflows.job_runtime import DurableJobSpec, DurableJobIdentity
    from src.memory.header_bounds import OPERATOR_SESSION
    await admission.header_budget.certify(db, OPERATOR_SESSION, (operator.session_id,))
    await _current_root(db, operator)
    candidate = admission.candidate()
    await certify_original_memory_principal(db, operator.principal.principal_id, admission.header_budget)
    principal = await authenticate_principal(operator.principal.principal_id, db=db)
    if (operator.principal.principal_id != candidate["operator_principal_id"]
        or operator.session_id != candidate["operator_session_id"] or principal.principal.grants != operator.principal.grants):
        raise NativeServiceBlocked("native_memory_original_owner_changed")
    job_id = memory_job_id(operator, candidate["method"], candidate["idempotency_key"])
    goal = candidate.get("source", {})
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, owner_kind="user",
        owner_principal_id=operator.principal.principal_id, job_kind=MEMORY_JOB_KIND, capability_version="1",
        idempotency_scope="native-memory-mutation", idempotency_key=candidate["idempotency_key"]),
        inputs=candidate, session_id=operator.session_id, operator_session_id=operator.session_id,
        conversation_id=operator.session_id, composition_binding=binding,
        goal_id=goal.get("goal_id"), goal_revision=goal.get("goal_revision"),
        declared_authority={"principal": operator.principal.principal_id, "owner_kind": "user", "session_id": operator.session_id,
            "grants": sorted(str(getattr(grant, "value", grant)) for grant in principal.principal.grants)},
        deadline_at=datetime.fromisoformat(candidate["original_deadline"]), max_attempts=1,
        run_fingerprint=_digest({"candidate": admission.candidate_digest, "binding": binding.binding_digest}))
    await validate_memory_spec(db, spec, admission)
    return spec


def memory_job_id(operator, method, key):
    return "native-memory:" + _digest({"principal": operator.principal.principal_id,
        "session": operator.session_id, "method": method, "key": key})


async def certify_original_memory_principal(db, principal_id, budget):
    """Numeric cover for the original principal and reverse-link projections.

    Conservative full OperatorSession inventory avoids a second authentication
    selector or authority path. Original authenticate_principal still decides
    which live root and ownership metadata qualify.
    """
    from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION
    if (type(budget) is not HeaderReadBudget or type(principal_id) is not str
            or not 1 <= len(principal_id.encode("utf-8")) <= 512):
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    certificate = await budget.certify_all(db, OPERATOR_SESSION, _header_budget=budget)
    # _ownership_metadata retrieves at most two non-tombstone ids and one
    # impossible tombstone id. Reserve their complete JSON string appearances
    # independently of the full root projection certified above.
    budget.debit(3 * (6 * 512 + 2) + 2)
    _native_memory_authority_owner_pay(db, "principal-certificate", certificate=certificate)
    return certificate


def original_request(candidate):
    """Compare a retry's original authenticated input without renewing staging."""
    method = candidate["method"]
    if method == "memory.propose":
        return {key: candidate["source"][key] for key in ("task_id", "expected_task_revision", "attempt_id")}
    if method == "memory.forget":
        return {key: candidate[key] for key in ("record_ref", "mode", "privacy_boundary", "reason")}
    return {**{key: candidate[key] for key in ("proposal_id", "action", "expected_revision",
        "expected_preview_text_digest", "edited_text", "decision_effect", "preferred_capability_id", "corrects_memory_id", "reason")},
        "expected_task_revision": candidate["source"]["expected_task_revision"],
        "expected_goal_revision": candidate["source"]["goal_revision"]}


async def execute_native_memory(*, operator, method, request, idempotency_key):
    from src.db.engine import get_session
    from src.db.models import WorkflowRunState
    from src.auth.ownership import _current_root
    from src.workflows.job_runtime import durable_job_repository as jobs, DurableJobError
    from .bridge import cordis_host as host, HostBlocked
    from .ownership import begin_native_writer, bind_invocation
    if (method not in MEMORY_METHODS or not host.admitting or host.reviewed is None
        or type(idempotency_key) is not str or not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key)):
        raise NativeServiceBlocked("native_memory_original_host_unavailable")
    from src.workspace import accounting_witness
    if any(not callable(getattr(accounting_witness, name, None)) for name in
        ("begin_native_memory_effect", "capture_native_memory_retention", "validate_native_memory_retention")):
        raise NativeServiceBlocked("native_memory_retention_unavailable")
    reviewed, boot = host.reviewed, host.boot_nonce
    job_id = memory_job_id(operator, method, idempotency_key)
    from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION, WRS_BY_RUN
    from src.memory.composition_headers import locate_exact_rows
    budget = HeaderReadBudget()
    async with get_session() as db:
        from sqlalchemy import text
        connection = await db.connection()
        if not (await connection.get_raw_connection()).driver_connection.in_transaction:
            await db.execute(text("BEGIN"))
        await budget.certify(db, OPERATOR_SESSION, (operator.session_id,))
        await _current_root(db, operator)
        prior_ids = await locate_exact_rows(db, WRS_BY_RUN, job_id, budget)
        prior = None
        if prior_ids:
            await budget.certify(db, WRS_BY_RUN, prior_ids)
            prior = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
        if prior is not None:
            context = memory_context(prior)
            if (context["candidate"]["operator_principal_id"] != operator.principal.principal_id
                or context["candidate"]["operator_session_id"] != operator.session_id
                or original_request(context["candidate"]) != request):
                raise NativeServiceBlocked("native_memory_idempotency_input_changed")
            # Never private result release or another call from a fresh request.
            return {"job": await jobs.get_job(job_id, header_budget=budget), "replayed": True, "memory_status": "no_learning"}
        binding = await bind_invocation(db, method=method, native_branch="base",
            goal_bound=method != "memory.forget", reviewed_composition=reviewed, header_budget=budget)
        admission = await prepare_memory_admission(db, operator=operator, method=method, request=request,
            idempotency_key=idempotency_key, host_boot_nonce=boot, composition_binding_digest=binding.binding_digest,
            original_deadline=datetime.now(timezone.utc) + timedelta(seconds=30), header_budget=budget)
        await db.rollback()  # Staged Vault I/O is complete before BEGIN.
        await begin_native_writer(db, owner="durable_jobs", header_budget=budget)
        async def admission_check(writer, _run):
            await admission.header_budget.certify(writer, OPERATOR_SESSION, (operator.session_id,))
            await _current_root(writer, operator)
            await validate_original_memory_owner(writer, admission)
        spec = await native_memory_spec(db, admission=admission, operator=operator, binding=binding)
        job = await jobs._admit_in_session(db, spec, native_memory_admission=admission,
            native_memory_host=host, native_memory_host_boot_nonce=boot,
            admission_authority_check=admission_check)
    if job["status"] != "accepted":
        return {"job": job, "replayed": True, "memory_status": "no_learning"}
    claim = None
    scope = None
    try:
        await jobs.transition_job(job_id, "queued", expected_revision=job["revision"], header_budget=budget)
        claim = await jobs.claim_service_job(job_id, host=host, owner="native-memory:" + boot[:24],
            lease_seconds=30, claim_authority_check=admission_check, header_budget=budget,
            native_memory_admission=admission)
        scope = _issue_memory_dispatch_source(claim, admission, operator, host)
        result = await host.request_service(method, admission.wire_inputs(), original_scope=scope)
        completed = await jobs.complete_native_memory(claim, result=result, header_budget=budget)
        return {"job": completed, "result": result, "replayed": False, "memory_status": result["memory_status"]}
    except (NativeServiceBlocked, HostBlocked, DurableJobError) as exc:
        if claim is not None:
            witness = claim.checkpoint["payload"]
            try:
                await jobs.transition_job(job_id, "blocked", owner=witness["lease_owner"],
                    fencing_token=witness["fencing_token"], reason=getattr(exc, "reason_code", "native_memory_execution_blocked"))
            except DurableJobError:
                pass
        raise
    finally:
        if scope is not None:
            scope.native_memory_source.close()


async def native_memory_http(**kwargs):
    from fastapi import HTTPException
    from src.workflows.job_runtime import DurableJobError
    from src.auth.service import AuthFailure
    from src.work_board.repository import BoardError
    from .ownership import CompositionBindingError
    from src.memory.header_bounds import HeaderBoundsError
    try:
        return await execute_native_memory(**kwargs)
    except HeaderBoundsError as exc:
        raise HTTPException(status_code=409, detail={"code": "canonical_bound_not_certified"}) from exc
    except (NativeServiceBlocked, DurableJobError, AuthFailure, BoardError, CompositionBindingError, ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail={"code": getattr(exc, "reason_code", "native_memory_original_unavailable")}) from exc


def _native_memory_dispatch_result_upper(stage, widths, run_identity, claim_ref):
    """Closed original result/readback allocations, without future values."""
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if stage.startswith("receipt-seed-"):
        return (2 + 2 + _native_memory_ascii_string_width("invocation") + 1
            + _native_memory_ascii_string_width(run_identity)
            + _native_memory_ascii_string_width("claim") + 1
            + _native_memory_ascii_string_width(claim_ref)
            + _native_memory_ascii_string_width("candidate") + 1 + 66)
    if stage == "receipt-digest":
        return 66
    if stage == "receipt-locator":
        return _native_memory_ascii_string_width("native-memory-receipt:") + 64
    if stage in {"result-value-json", "result-value-update", "protocol-result", "protocol-grammar", "protocol-fields",
            "protocol-value-fields", "protocol-key-views", "protocol-items-views",
            "result-canonical", "result-utf8"}:
        return widths["result_upper"]
    if stage == "result-digest":
        return 66
    if stage in {"effect-asdict-fields", "effect-asdict"}:
        return widths["effect_upper"]
    if stage == "expected-context-result":
        return widths["context_upper"]
    raise HeaderBoundsError("memory_dispatch_result_numeric_stage_unavailable")


def original_native_memory_dispatch_result_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE original dispatch's post-effect result and capture readback copies.

    Authority callbacks, capture itself and nested Source context/locator
    validators have separate owners. This contribution issues no ledger.
    """
    from src.workspace.accounting_witness import _native_memory_generic_future_bound_inputs
    _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, retention_widths)
    from src.memory.header_bounds import _native_memory_ascii_string_width as string_width
    # The longest source stage is receipt-seed-construction (24 ASCII bytes).
    # At most 21 entries are emitted (15 for propose). Each nested triple
    # contains its literal phase, two-item appearance and int64 amount. Pay
    # emitted structures, the entries list, and the returned tuple separately
    # before any of these allocations; unused capacity remains spent.
    label_upper = 3 + string_width("native-memory-dispatch-result") + 26
    triple_upper = 4 + string_width("capture") + label_upper + 20
    vector_upper = 2 + 20 + 21 * triple_upper
    for storage in ("entry-structures", "entries-list", "return-tuple"):
        certificate.budget.debit(vector_upper,
            appearance=("native-memory-dispatch-result-planning", storage))
    def entry(phase, stage):
        return (phase, ("native-memory-dispatch-result", stage),
            _native_memory_dispatch_result_upper(stage, retention_widths, operation.run.run_identity,
                operation.source._scope.witness["claim_ref"]))
    entries = [entry("effect", "result-value-json")]
    if prepared._method != "memory.propose":
        entries.extend(entry("effect", stage) for stage in ("receipt-seed-construction",
            "receipt-seed-canonical", "receipt-seed-utf8", "receipt-digest", "receipt-locator",
            "result-value-update"))
    entries.extend(entry("effect", stage) for stage in ("protocol-result", "protocol-grammar", "protocol-fields",
        "protocol-value-fields", "protocol-key-views", "protocol-key-views",
        "protocol-items-views", "protocol-items-views"))
    entries.extend(entry("capture", stage) for stage in ("result-canonical", "result-utf8",
        "result-digest", "effect-asdict-fields", "effect-asdict", "expected-context-result"))
    return tuple(entries)


def _native_memory_dispatch_result_pay(retention, db, phase, stage):
    import asyncio
    from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, HeaderBoundsError
    from src.workspace.accounting_witness import _NativeMemoryRetention
    if type(retention) is not _NativeMemoryRetention:
        raise HeaderBoundsError("memory_dispatch_result_numeric_owner_changed")
    retention._checked()
    ledger = retention.ledger
    ledger._checked()
    active = _MEMORY_LEDGER_PHASE.get()
    lineage = getattr(retention, "_publication_numeric_facts", None)
    binding = ledger.bindings.get(phase)
    if (active is None or active[0] is not ledger or active[1] != phase
            or binding is None or binding[0] is not db
            or binding[1] is not db.sync_session.get_transaction()
            or binding[5] is not asyncio.current_task() or not binding[2].in_transaction
            or lineage is None
            or lineage[0] is not retention.operation or lineage[1] is not retention.prepared):
        raise HeaderBoundsError("memory_dispatch_result_numeric_owner_changed")
    upper = _native_memory_dispatch_result_upper(stage, lineage[3], retention.operation.run.run_identity,
        retention.source._scope.witness["claim_ref"])
    ledger.budget.debit(upper, appearance=("native-memory-dispatch-result", stage))


async def dispatch_memory_mutation(dispatcher, db, run, witness, method, payload, original_scope):
    from src.workspace.accounting_witness import begin_native_memory_effect, capture_native_memory_retention
    from .contracts import succeeded, blocked
    from dataclasses import asdict
    context = memory_context(run)
    source = await _validate_memory_dispatch_source(db, run, original_scope)
    # The original admission carries the operation's cumulative read budget
    # and staged producer. Candidate JSON cannot reconstruct either object.
    admission = source.admission
    if (context["result"] is not None or method != context["candidate"]["method"]
        or payload != admission.wire_inputs()):
        raise NativeServiceBlocked("native_memory_original_inputs_changed")
    async def authority_original(writer):
        from .dispatch import _witness, _ms
        from .bridge import cordis_host as current_host
        from .ownership import validate_invocation
        from src.auth.service import authenticate_principal
        from src.db.models import OperatorSession
        from src.workflows.job_runtime import _assert_canonical_goal_fence
        from src.memory.header_bounds import _native_memory_utf8_width
        witness_upper = _native_memory_dispatch_witness_upper(original_scope.witness)
        for stage, upper in _native_memory_dispatch_witness_entries(
                _native_memory_utf8_width(run.checkpoint_receipts_json or "[]"), witness_upper):
            admission.header_budget.debit(upper, appearance=("native-memory-authority", stage))
        for stage, upper in (("scope-witness-copy", witness_upper),
                ("deadline-replace", 44), ("deadline-astimezone", 44), ("deadline-now", 44)):
            admission.header_budget.debit(upper, appearance=("native-memory-authority", stage))
        current_witness = _witness(run, original_scope)
        if (not current_host.admitting or current_host.reviewed is None
            or current_host.boot_nonce != original_scope.host_boot_nonce
            or current_host.reviewed.package_digest != witness["package_digest"]
            or current_host.reviewed.composition_digest != witness["host_composition_digest"]
            or current_witness != witness or dict(original_scope.witness) != witness
            or run.status != "running" or run.lease_owner != witness["lease_owner"]
            or run.fencing_token != witness["fencing_token"] or run.attempt_count != witness["attempt_count"]
            or run.input_digest != witness["input_digest"] or run.authority_digest != witness["authority_digest"]
            or run.run_fingerprint != witness["run_fingerprint"]
            or _ms(run.deadline_at) != witness["original_deadline_at"]
            or _ms(run.deadline_at) <= int(datetime.now(timezone.utc).timestamp() * 1000)):
            raise NativeServiceBlocked("native_memory_original_claim_changed")
        dispatcher.jobs._assert_lease(run, owner=witness["lease_owner"], fencing_token=witness["fencing_token"])
        await validate_invocation(writer, original_scope.binding, header_budget=admission.header_budget)
        from src.memory.header_bounds import OPERATOR_SESSION
        root_certificate = await admission.header_budget.certify(writer, OPERATOR_SESSION,
            (run.operator_session_id,), _header_budget=admission.header_budget)
        admission.header_budget.debit(44, appearance=("native-memory-authority", "root-now"))
        admission.header_budget.debit(root_certificate.upper_bytes,
            appearance=("native-memory-authority", "root-query"))
        admission.header_budget.debit(root_certificate.upper_bytes,
            appearance=("native-memory-authority", "root-materialization"))
        now = datetime.now(timezone.utc)
        root = await writer.scalar(select(OperatorSession).where(
            OperatorSession.id == run.operator_session_id, OperatorSession.principal_id == run.owner_principal_id,
            OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
            OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
            OperatorSession.absolute_expires_at > now).execution_options(populate_existing=True))
        await certify_original_memory_principal(writer, run.owner_principal_id, admission.header_budget)
        current = await authenticate_principal(run.owner_principal_id, db=writer)
        authority_upper = _native_memory_source_capacity(
            6 * _native_memory_utf8_width(run.declared_authority_json) + 2)
        from src.security.trust_contract import AuthorityGrant
        grants_upper = 2 + 3 + sum(_native_memory_source_value_upper(grant.value) for grant in (
            AuthorityGrant.INGRESS, AuthorityGrant.MODEL_INFERENCE,
            AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.ARTIFACT_TRANSFER))
        for stage, upper in (("authority-json-parser", authority_upper),
                ("grant-generator", grants_upper), ("grant-sort-list", grants_upper)):
            admission.header_budget.debit(upper, appearance=("native-memory-authority", stage))
        if (root is None or context["candidate"]["host_boot_nonce"] != original_scope.host_boot_nonce
            or json.loads(run.declared_authority_json).get("grants") != sorted(
                str(getattr(grant, "value", grant)) for grant in current.principal.grants)):
            raise NativeServiceBlocked("native_memory_original_root_changed")
        if run.goal_id is not None:
            from src.memory.header_bounds import GOAL
            goal_certificate = await admission.header_budget.certify(writer, GOAL, (run.goal_id,),
                _header_budget=admission.header_budget)
            for stage, upper in (("goal-query", goal_certificate.upper_bytes),
                    ("goal-materialization", goal_certificate.upper_bytes),
                    ("goal-owner-session-tuple", goal_certificate.upper_bytes),
                    ("goal-authority-json-parser", authority_upper)):
                admission.header_budget.debit(upper, appearance=("native-memory-authority", stage))
        await _assert_canonical_goal_fence(writer, goal_id=run.goal_id, goal_revision=run.goal_revision,
            owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
            session_id=run.session_id, authority=run.declared_authority_json)
    async def authority(writer):
        import asyncio
        from src.memory.header_bounds import HeaderBoundsError, _native_memory_object_width
        if _MEMORY_AUTHORITY_CAPACITY.get() is not None:
            raise HeaderBoundsError("memory_authority_numeric_owner_changed")
        connection = await writer.connection()
        raw = await connection.get_raw_connection()
        if not raw.driver_connection.in_transaction:
            raise HeaderBoundsError("memory_authority_numeric_owner_changed")
        upper = _native_memory_object_width({name: 4 for name in (
            "source", "scope", "db", "transaction", "driver", "task", "budget",
            "runtime_certificate", "principal_certificate")})
        admission.header_budget.debit(upper, appearance=("native-memory-authority", "capacity-owner"))
        owner = _MemoryAuthorityCapacity(source, original_scope, writer,
            writer.sync_session.get_transaction(), raw.driver_connection, asyncio.current_task(),
            admission.header_budget)
        _MEMORY_AUTHORITY_OWNERS[id(owner)] = owner
        token = _MEMORY_AUTHORITY_CAPACITY.set(owner)
        trace_token = collector_token = None
        collector = None
        try:
            _native_memory_authority_capacity_checked(writer)
            from src.memory.header_bounds import _MEMORY_CHARGE_TRACE, _MEMORY_LEDGER_PHASE
            guard = writer.info["composition_guard"]
            if (_MEMORY_LEDGER_PHASE.get() is None
                    and getattr(guard, "_native_memory_authority_read_numeric", None) is None):
                admission.header_budget.debit(2,
                    appearance=("native-memory-authority-trace", "collector-list"))
                admission.header_budget.debit(_native_memory_object_width(
                    {"owner": 4, "prior": 4, "entries": 2}),
                    appearance=("native-memory-authority-trace", "collector-constructor"))
                collector = _MemoryAuthorityTraceCollector(owner, _MEMORY_CHARGE_TRACE.get())
                _MEMORY_AUTHORITY_TRACE_COLLECTORS[id(collector)] = collector
                collector_token = _MEMORY_AUTHORITY_TRACE_ACTIVE.set(collector)
                trace_token = _MEMORY_CHARGE_TRACE.set((admission.header_budget, collector))
            await authority_original(writer)
            if collector is not None:
                from src.memory.composition_headers import _common_trace_label_width
                vector_upper = (2 + max(0, len(collector.entries) - 1)
                    + sum(3 + _common_trace_label_width(appearance) + 20
                        for appearance, amount in collector.entries))
                collector._storage_debit(vector_upper, "retained-tuple")
                roster_upper = (_native_memory_source_value_upper(owner.runtime_certificate.row_ids)
                    + _native_memory_source_value_upper(owner.principal_certificate.row_ids))
                collector._storage_debit(vector_upper + roster_upper + 7 * 4 + 11, "retained-lineage")
                guard._native_memory_authority_read_numeric = (
                    source, original_scope, writer, owner.transaction, owner.driver, owner.task,
                    owner.budget, tuple(collector.entries), owner.runtime_certificate.row_ids,
                    owner.principal_certificate.row_ids)
        finally:
            if trace_token is not None:
                _MEMORY_CHARGE_TRACE.reset(trace_token)
            if collector_token is not None:
                _MEMORY_AUTHORITY_TRACE_ACTIVE.reset(collector_token)
            if collector is not None:
                _MEMORY_AUTHORITY_TRACE_COLLECTORS.pop(id(collector), None)
            _MEMORY_AUTHORITY_CAPACITY.reset(token)
            _MEMORY_AUTHORITY_OWNERS.pop(id(owner), None)
    from src.memory import m5
    await authority(db)
    report_context = None
    if admission.candidate().get("source", {}).get("capability_id") == REPORT_SOURCE_CAPABILITY:
        report_context = _MemoryReportReadContext(admission.report_source, admission, db, authority)
    prepared = await m5.prepare_native_memory_mutation(db, admission=admission,
        native_memory_report_source=report_context,
        native_memory_source=source if report_context is not None else None)
    operation = await _begin_memory_owner_source(db, run, original_scope, prepared=prepared)
    retention_owner = await begin_native_memory_effect(db, run, witness["claim_ref"], admission,
        source_operation=operation, prepared=prepared)
    from src.workspace.accounting_witness import _NativeMemoryRetention
    if type(retention_owner) is not _NativeMemoryRetention:
        raise NativeServiceBlocked("native_memory_retention_unavailable")
    async with retention_owner.phase("effect", db, run):
        effect = await perform_memory_mutation(db, admission, authority_check=authority,
            source_operation=operation, native_prepared_mutation=prepared,
            native_memory_report_source=report_context)
        if effect.status == "succeeded":
            _native_memory_dispatch_result_pay(retention_owner, db, "effect", "result-value-json")
            value = json.loads(effect.value_json)
            if method != "memory.propose":
                for stage in ("receipt-seed-construction", "receipt-seed-canonical", "receipt-seed-utf8",
                        "receipt-digest", "receipt-locator", "result-value-update"):
                    _native_memory_dispatch_result_pay(retention_owner, db, "effect", stage)
                value["receipt_ref"] = "native-memory-receipt:" + _digest({"invocation": run.run_identity,
                    "claim": witness["claim_ref"], "candidate": admission.candidate_digest})
            for stage in ("protocol-result", "protocol-grammar", "protocol-fields", "protocol-value-fields",
                    "protocol-key-views", "protocol-key-views", "protocol-items-views", "protocol-items-views"):
                _native_memory_dispatch_result_pay(retention_owner, db, "effect", stage)
            result = succeeded(method, value)
        else:
            result = blocked("native_memory_owner_outcome_unsupported")
    # Capture stages the complete actual owner result before computing Current.
    # Appending this context after capture would immediately stale its own WRS.
    async with retention_owner.phase("capture", db, run):
        capture = await capture_native_memory_retention(db, run, witness["claim_ref"], effect,
            source_operation=operation, prepared=prepared, result=result)
        retention = validate_memory_retention_locator(capture.payload(), run.run_identity)
        for stage in ("result-canonical", "result-utf8", "result-digest", "effect-asdict-fields", "effect-asdict", "expected-context-result"):
            _native_memory_dispatch_result_pay(retention_owner, db, "capture", stage)
        expected = {"invocation_ref": run.run_identity, "claim_ref": witness["claim_ref"],
            "candidate_digest": admission.candidate_digest, "result_digest": _digest(result), "payload": result,
            "effect": asdict(effect), "retention": retention}
        if memory_context(run)["result"] != expected:
            raise NativeServiceBlocked("native_memory_retention_result_readback_changed")
        await authority(db)
    return result


@dataclass(frozen=True)
class NativeMemoryOwnerReference:
    table: str
    row_ref: str


def _native_memory_original_lookup_payer(db, operation):
    """Original registered writer resource custody; never locator permission."""
    import asyncio
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
    from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
    from src.workspace.accounting_witness import CompositionSessionGuard, _COMPOSITION_METADATA_GUARDS
    if (type(operation) is not _MemoryOwnerSourceOperation
            or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or operation._seal is not _MEMORY_SOURCE_SEAL or operation._issued_id != id(operation)
            or operation.db is not db):
        raise HeaderBoundsError("memory_original_lookup_payer_changed")
    source = operation.source
    if (type(source) is not _NativeMemoryDispatchSource or _MEMORY_SOURCES.get(id(source)) is not source
            or source._seal is not _MEMORY_SOURCE_SEAL or source._issued_id != id(source)
            or not source._live.is_set() or operation.scope is not source._scope
            or source._scope.native_memory_source is not source):
        raise HeaderBoundsError("memory_original_lookup_payer_changed")
    from src.memory.m5 import PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS, _NATIVE_MUTATION_PLAN_SEAL
    plan = operation._prepared
    if (type(plan) is not PreparedNativeMemoryMutation
            or _NATIVE_MUTATION_PLANS.get(id(plan)) is not plan
            or plan._seal is not _NATIVE_MUTATION_PLAN_SEAL or plan._db is not db
            or plan._transaction is not operation.transaction or plan._driver is not operation.driver
            or plan._candidate_digest != source.admission.candidate_digest or plan.consumed):
        raise HeaderBoundsError("memory_original_lookup_payer_changed")
    guard = db.info.get("composition_guard")
    custody = getattr(guard, "_native_memory_authority_read_numeric", None)
    common = getattr(guard, "_native_memory_source_common_numeric", None)
    frame = source.admission.header_budget
    if (type(guard) is not CompositionSessionGuard or guard not in _COMPOSITION_METADATA_GUARDS
            or guard.db is not db or guard._retention_closed or guard.header_budget is not frame
            or type(frame) is not HeaderReadBudget or not db.info.get("native_writer_started")
            or type(custody) is not tuple or len(custody) != 10 or custody[0] is not source
            or custody[1] is not source._scope or custody[2] is not db
            or custody[3] is not operation.transaction or custody[4] is not operation.driver
            or custody[5] is not asyncio.current_task() or custody[6] is not frame
            or db.sync_session.get_transaction() is not operation.transaction
            or not operation.driver.in_transaction or not source.host.admitting
            or source.claim._host is not source.host or source.host.boot_nonce != source.claim.host_boot_nonce
            or type(common) is not tuple or len(common) != 3 or common[0] is not operation):
        raise HeaderBoundsError("memory_original_lookup_payer_changed")
    certificate = common[1]
    if (type(certificate) is not CompositionHeaderCertificate or certificate.seal is not _SEAL
            or certificate.issued_id != id(certificate) or certificate.budget is not frame
            or certificate.driver is not operation.driver
            or certificate.changes != operation.driver.total_changes
            or not certificate.connection.in_transaction()
            or certificate.connection.get_transaction() is not certificate.transaction):
        raise HeaderBoundsError("memory_original_lookup_payer_changed")
    from src.memory.composition_headers import current_budget
    current = current_budget()
    if current is not None and current is not frame:
        raise HeaderBoundsError("memory_original_lookup_payer_changed")
    return frame


async def _memory_locator_rows(db, model, fields, *predicates, limit=129,
        _native_memory_operation=None):
    """Closed owner locators only; never full proposal, alias or private bodies."""
    original_payer = (None if _native_memory_operation is None
        else _native_memory_original_lookup_payer(db, _native_memory_operation))
    from sqlalchemy import case, func
    allowed = {
        "goals": {"id", "parent_id", "owner_principal_id", "owner_session_id"},
        "memory_proposals": {"proposal_id", "owner_principal_id", "owner_session_id",
                             "recovered_from_proposal_id", "proposal_job_id", "corrects_memory_id"},
        "work_board_proposals": {"proposal_id", "owner_principal_id", "owner_session_id"},
        "work_board_decision_receipts": {"receipt_id"},
        "memories": {"id", "source_session_id", "subject_entity_id", "project_entity_id"},
        "memory_entities": {"id"}, "memory_sources": {"id"}, "memory_tombstones": {"id"},
        "memory_edges": {"id", "from_memory_id", "to_memory_id"},
    }
    if (type(fields) is not tuple or not fields or not set(fields) <= allowed.get(model.__tablename__, set())
            or type(limit) is not int or not 1 <= limit <= 129):
        raise NativeServiceBlocked("native_memory_retention_locator_unavailable")
    from src.memory.composition_headers import current_budget, _metadata_upper
    from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, _NativeMemoryNumericLedger
    payer = original_payer if original_payer is not None else current_budget()
    active = _MEMORY_LEDGER_PHASE.get()
    if active is not None:
        ledger, phase = active
        if type(ledger) is not _NativeMemoryNumericLedger:
            raise NativeServiceBlocked("native_memory_retention_locator_unavailable")
        from src.workspace.accounting_witness import _native_memory_capacity_slots
        _native_memory_capacity_slots(ledger)
        if ledger.bindings[phase][0] is not db or (payer is not None and payer is not ledger.budget):
            raise NativeServiceBlocked("native_memory_retention_locator_unavailable")
        payer = ledger.budget
    if payer is not None:
        # Read only numerical widths before the original bounded locator.
        # The same original predicate and limit retain its body permission.
        for stage in ("header-delivery", "header-row-copy", "header-encoded-copy"):
            payer.debit(_metadata_upper(limit, (20, 20) * len(fields)),
                appearance=("native-memory-locator", model.__tablename__, fields, stage))
        header_columns = []
        for name in fields:
            column = getattr(model, name)
            valid = case((column.is_(None), 1), (func.typeof(column) == "text",
                case((func.octet_length(column) <= 512, 1), else_=0)), else_=0)
            header_columns.extend((func.octet_length(column), valid))
        widths = list(await db.execute(select(*header_columns).where(*predicates).limit(limit)))
        # A saturated locator is already outside this owner's unique/128-ref
        # profiles. Stop before a possibly different truncated body subset;
        # an unsaturated header enumerates every matching row in this writer.
        if len(widths) >= limit or any(row[index + 1] != 1 or (row[index] is not None and
                (type(row[index]) is not int or not 0 <= row[index] <= 512))
                for row in widths for index in range(0, len(row), 2)):
            raise NativeServiceBlocked("native_memory_retention_locator_bound")
        row_upper = 2 + sum(3 + sum((4 if row[index] is None else 6 * row[index] + 2) + 3
            for index in range(0, len(row), 2)) for row in widths)
        for stage in ("delivery", "row-copy", "encoded-copy", "result-construction", "return"):
            payer.debit(row_upper + sum(6 * len(name) + 3 for name in fields) * len(widths),
                appearance=("native-memory-locator", model.__tablename__, fields, stage))
    columns = []
    for name in fields:
        column = getattr(model, name)
        bounded = case((func.typeof(column) == "text",
                        case((func.octet_length(column) <= 512, column))), else_=None)
        valid = case((column.is_(None), 1), (func.typeof(column) == "text",
                     case((func.octet_length(column) <= 512, 1), else_=0)), else_=0)
        columns.extend((bounded, valid))
    rows = list(await db.execute(select(*columns).where(*predicates).limit(limit)))
    result = []
    for row in rows:
        if any(row[index + 1] != 1 for index in range(0, len(row), 2)):
            raise NativeServiceBlocked("native_memory_retention_locator_bound")
        result.append(dict(zip(fields, row[::2])))
    return result


def original_native_memory_source_candidate_construction_numeric_upper(operation, prepared, *,
        certificate, facts, retention_widths):
    """Storage for ONE original candidate9 builder; no context or body fee."""
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    _native_memory_source_capacity(retention_widths["source_upper"])
    # Actual arrays: validation4, comparison3, final9; nine fresh labels/pairs.
    # Longest closed source/owner/stage strings each occupy <=22 JSON bytes.
    label = 4 + 3 * 22
    pair = 3 + label + 20
    return ((2 + 3 + 4 * pair) + (2 + 2 + 3 * pair)
        + (2 + 8 + 9 * pair) + 9 * (pair + label) + 2 * 41)


def original_native_memory_reference_lookup_construction_numeric_upper(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE original propose lookup forecast constructor, no queries/vectors."""
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    from src.memory.header_bounds import HeaderBoundsError
    if prepared._method != "memory.propose" or prepared._result.get("status") != "proposed":
        raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    guard = operation.db.info["composition_guard"]
    projection = getattr(guard, "_native_memory_source_projection", None)
    if type(projection) is not tuple or len(projection) != 3 or projection[0] is not operation:
        raise HeaderBoundsError("memory_lookup_numeric_source_changed")
    proposal_id = prepared._result.get("proposal_id")
    proposed = None
    proposal_count = 0
    for descriptor, key, row in projection[1]:
        if descriptor.table == "memory_proposals" and key == proposal_id:
            proposed = row
            proposal_count += 1
    if proposal_count != 1 or proposed["corrects_memory_id"] is not None or proposed["recovered_from_proposal_id"] is not None:
        raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    goals = 0
    for table, key in facts["row_uppers"]:
        if table == "goals":
            goals += 1
    board = int(proposed["proposal_job_id"] is not None)
    addresses = goals + 2 + board
    if not 2 <= addresses <= 128:
        raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    locators = goals + 2 + board
    entries = 38 + 12 * goals + 11 * board
    # Actual eight locator labels have four fields including the closed field
    # tuple (at most six ASCII column names, longest recovered_from_proposal_id26).
    field_tuple = 2 + 5 + 6 * (len("recovered_from_proposal_id") + 2)
    label = 5 + len("native-memory-locator") + 2 + len("work_board_decision_receipts") + 2 + field_tuple + 40 + 2
    pair = 3 + label + 20
    vector = 2 + entries - 1 + entries * pair
    result = 2 * vector + entries * (pair + label)  # entries list + return tuple + pairs/labels
    # Candidate9: validation4, comparison/digest3 and final9 arrays; all
    # source-owned appearance pairs/labels and the two original generators.
    result += original_native_memory_source_candidate_construction_numeric_upper(
        operation, prepared, certificate=certificate, facts=facts,
        retention_widths=retention_widths)
    # Actual address/seen sets and every constructed (table,key) tuple use
    # the existing locator's UTF8<=512 limit. No future identity is minted.
    address = 3 + len("work_board_decision_receipts") + 2 + 6 * 512 + 2
    result += 2 * (2 + addresses - 1 + addresses * address)
    result += 2 + max(0, goals - 1) + goals * (6 * 512 + 2)
    # Two original projection-selection generators/materialized tuples and
    # three helper closures plus the async call resource wrapper.
    result += 2 * (2 + len(projection[1]) - 1 + len(projection[1]) * 4)
    result += 6 * 41 + 3 * (2 + 5 + 6 * 4)
    # Each locator has <=one original row (LIMIT2 unsaturated), plus <=one
    # prospective row from the authentic plan. Cover both before its original
    # len(rows)>=2 STOP, with six selected fields and real512-byte text cap.
    row = 2 + 5 + 6 * (len("recovered_from_proposal_id") + 3 + 6 * 512 + 2)
    trace = 2 + 7 + 8 * pair
    per_locator = 2 * trace + 8 * pair  # observed trace list/pairs and stored tuple view
    per_locator += 2 * (2 + 1 + 2 * row) + row  # combined copy/list + projected field map
    per_locator += 2 + 1 + 2 * 4  # original old+projected splice tuple
    per_locator += 40 * 41  # copy/row/field/collision and string-width generators
    per_locator += 2 * (2 + 5 + 6 * 4) + 41 + 11 + 8 + 2  # trace scope/async closure/token
    result += locators * per_locator
    # Final numeric-vector width walk: one list sum + per-pair/per-label tuple
    # sums + at most twelve ASCII string walkers per original locator label.
    result += (1 + 14 * entries) * 41
    return result


async def original_native_memory_reference_lookup_numeric_schedule(operation, prepared, *, certificate, facts):
    """Close the original propose locators from paid rows and its sealed plan.

    These are numerical liabilities only. No future owner reference or read
    certificate is constructed. Existing locator queries retain their original
    predicates and permissions; a prospective result comes only from the
    exact original plan already retained by the Source.
    """
    from src.memory.header_bounds import HeaderBoundsError
    from src.workspace.accounting_witness import CompositionSessionGuard
    if type(operation) is not _MemoryOwnerSourceOperation or _MEMORY_OPERATIONS.get(id(operation)) is not operation:
        raise HeaderBoundsError("memory_lookup_numeric_source_changed")
    guard = operation.db.info.get("composition_guard")
    widths_owner = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if type(guard) is not CompositionSessionGuard or type(widths_owner) is not tuple or len(widths_owner) != 4:
        raise HeaderBoundsError("memory_lookup_numeric_source_changed")
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths_owner[3])
    from src.db.models import Goal, MemoryProposal, WorkBoardProposal, WorkBoardDecisionReceipt, WorkBoardDecisionReceiptStage
    from src.memory.header_bounds import (
        HeaderBoundsError, _trace_memory_numeric_charges,
        _native_memory_ascii_string_width, _native_memory_utf8_width)
    from src.workspace.accounting_witness import CompositionSessionGuard, _native_memory_closed_value_width
    source = operation.source
    guard = operation.db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_source_capacity_facts", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    if (type(guard) is not CompositionSessionGuard or operation._prepared is not prepared
            or prepared.consumed or operation._effect is not None
            or retained is None or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not facts
            or projection is None or projection[0] is not operation
            or certificate.budget is not source.admission.header_budget):
        raise HeaderBoundsError("memory_lookup_numeric_source_changed")
    original_lookup = getattr(guard, "_native_memory_reference_lookup_original", None)
    import asyncio
    existing_rows = getattr(guard, "_native_memory_existing_source_rows", None)
    if (type(original_lookup) is not tuple or len(original_lookup) != 7
            or original_lookup[0] is not operation or original_lookup[1] is not certificate.budget
            or original_lookup[2] is not asyncio.current_task()
            or original_lookup[3] is not operation.transaction
            or original_lookup[4] is not operation.driver
            or original_lookup[6] is not source
            or existing_rows is None or existing_rows[0] is not operation):
        raise HeaderBoundsError("memory_lookup_original_numeric_unavailable")
    for reference in original_lookup[5]:
        found = False
        for table, key in existing_rows[1]:
            if table == reference.table and key == reference.row_ref:
                found = True
        if not found or (reference.table, reference.row_ref) not in certificate.rows:
            raise HeaderBoundsError("memory_lookup_original_numeric_unavailable")
    value = source.admission.candidate()
    if (prepared._method != "memory.propose" or value["method"] != prepared._method
            or prepared._result.get("status") != "proposed"
            or value.get("corrects_memory_id") is not None):
        raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    widths_owner = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if (widths_owner is None or widths_owner[0] is not operation
            or widths_owner[1] is not prepared or widths_owner[2] is not certificate):
        raise HeaderBoundsError("memory_lookup_numeric_source_changed")
    from src.runtime_plugins.memory_producer import original_native_memory_source_candidate_numeric_entries
    certificate.budget.debit(2,
        appearance=("memory-source-capacity", "lookup-numeric-setup"))
    entries = []
    entries.extend(original_native_memory_source_candidate_numeric_entries(
        candidate_upper=widths_owner[3]["source_upper"]))
    string = _native_memory_ascii_string_width
    def pay(stage, upper):
        entries.append((("native-memory-reference-lookup", stage), upper))
    # This arithmetic roster is private capacity, not NativeMemoryOwnerReference.
    addresses = set()
    def add(table, key):
        if type(key) is not str or not key or _native_memory_utf8_width(key) > 512:
            raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
        pay("reference-key-utf8", _native_memory_utf8_width(key))
        pay("reference-membership-tuple", 3 + string(table) + string(key))
        pay("reference-add", 3 + string(table) + string(key))
        addresses.add((table, key))
        if len(addresses) > 128:
            raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    async def locate(model, fields, predicates, *, projected):
        # Reuse only rows already read by the original Source owner. No SQL,
        # locator callback or future certificate is produced by this forecast.
        old = tuple(raw for descriptor, key, raw, _encoded in existing_rows[2]
            if descriptor.table == model.__tablename__
            and all(raw[name] == expected for name, expected in predicates))
        copy_upper = 2 + sum(3 + sum(string(name) + 1 +
            (4 if row[name] is None else string(row[name])) + 1 for name in fields)
            for row in (*old, *projected))
        certificate.budget.debit(copy_upper,
            appearance=("memory-source-capacity", "lookup-projection-copy", model.__tablename__, fields))
        rows = list(old)
        for raw in projected:
            if any(all(row[name] == raw[name] for name in fields) for row in old):
                raise HeaderBoundsError("memory_lookup_numeric_new_row_collision")
            rows.append({name: raw[name] for name in fields})
        if len(rows) >= 2:
            raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
        # Keep the exact original locator formula and every actual occurrence.
        row_upper = 2 + sum(3 + sum((4 if row[name] is None else
            6 * _native_memory_utf8_width(row[name]) + 2) + 3 for name in fields) for row in rows)
        result_upper = row_upper + sum(6 * len(name) + 3 for name in fields) * len(rows)
        from src.memory.composition_headers import _metadata_upper
        for stage in ("header-delivery", "header-row-copy", "header-encoded-copy"):
            entries.append((("native-memory-locator", model.__tablename__, fields, stage),
                _metadata_upper(2, (20, 20) * len(fields))))
        for stage in ("delivery", "row-copy", "encoded-copy", "result-construction", "return"):
            entries.append((("native-memory-locator", model.__tablename__, fields, stage), result_upper))
        return rows
    pay("reference-set", 2)
    goal_id = value["source"]["goal_id"]
    pay("goal-seen-set", 2)
    seen = set()
    while goal_id is not None:
        if goal_id in seen:
            raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
        pay("goal-seen-add", string(goal_id))
        seen.add(goal_id)
        rows = await locate(Goal, ("id", "parent_id", "owner_principal_id", "owner_session_id"),
            (("id", goal_id),), projected=())
        if (len(rows) != 1 or rows[0]["owner_principal_id"] != value["operator_principal_id"]
                or rows[0]["owner_session_id"] != value["operator_session_id"]):
            raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
        add("goals", rows[0]["id"])
        goal_id = rows[0]["parent_id"]
    pay("memory-id-set", 2)
    proposal_id = prepared._result["proposal_id"]
    proposed = tuple(raw for descriptor, key, raw in projection[1]
        if descriptor.table == "memory_proposals" and key == proposal_id)
    if len(proposed) != 1:
        raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    rows = await locate(MemoryProposal, ("proposal_id", "owner_principal_id", "owner_session_id",
        "recovered_from_proposal_id", "proposal_job_id", "corrects_memory_id"),
        (("proposal_id", proposal_id),), projected=proposed)
    proposal = rows[0]
    if (proposal["owner_principal_id"] != value["operator_principal_id"]
            or proposal["owner_session_id"] != value["operator_session_id"]
            or proposal["recovered_from_proposal_id"] is not None
            or proposal["corrects_memory_id"] is not None):
        raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    add("memory_proposals", proposal_id)
    if proposal["proposal_job_id"] is not None:
        board = await locate(WorkBoardProposal, ("proposal_id", "owner_principal_id", "owner_session_id"),
            (("admission_job_id", proposal["proposal_job_id"]),), projected=tuple(
                raw for descriptor, _key, raw in projection[1]
                if descriptor.table == "work_board_proposals"
                and raw["admission_job_id"] == proposal["proposal_job_id"]))
        if (len(board) != 1 or board[0]["owner_principal_id"] != value["operator_principal_id"]
                or board[0]["owner_session_id"] != value["operator_session_id"]):
            raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
        add("work_board_proposals", board[0]["proposal_id"])
    baselines = tuple(raw for descriptor, _key, raw in projection[1]
        if descriptor.table == "work_board_decision_receipts" and raw["source_proposal_id"] == proposal_id
        and raw["receipt_stage"] == WorkBoardDecisionReceiptStage.source_baseline.value)
    rows = await locate(WorkBoardDecisionReceipt, ("receipt_id",),
        (("source_proposal_id", proposal_id),
         ("receipt_stage", WorkBoardDecisionReceiptStage.source_baseline.value)), projected=baselines)
    if len(rows) != 1:
        raise HeaderBoundsError("memory_lookup_numeric_graph_unavailable")
    add("work_board_decision_receipts", rows[0]["receipt_id"])
    pay("memory-id-sort", 2)
    upper = 2 + sum(4 + string(table) + string(key) for table, key in addresses)
    for stage in ("reference-sort", "reference-dto", "reference-return"):
        pay(stage, upper)
    certificate.budget.debit(_native_memory_closed_value_width(entries),
        appearance=("memory-source-capacity", "lookup-numeric-return"))
    return tuple(entries)


async def memory_owner_references(db, admission, effect):
    """Post-effect locators; the capture owner separately verifies its issuer."""
    if type(admission) is not NativeMemoryMutationAdmission or type(effect) is not MemoryOwnerEffect:
        raise NativeServiceBlocked("native_memory_retention_original_effect_required")
    if effect.method != admission.candidate()["method"] or effect.candidate_digest != admission.candidate_digest:
        raise NativeServiceBlocked("native_memory_retention_original_effect_changed")
    return await _memory_existing_reference_lookup(db, admission,
        proposal_id=effect.proposal_id, record_id=effect.record_id,
        require_baseline=effect.status == "succeeded")


async def prospective_memory_existing_references(db, run, source_operation):
    """Actual Source's existing selectors, without pretending an effect exists.

    Future owner rows/bytes still require their original pre-effect reservation.
    This helper alone cannot admit mutation or certify a prospective closure.
    """
    source = await _validate_memory_owner_source(db, run, source_operation)
    value = source.admission.candidate()
    references = await _memory_existing_reference_lookup(db, source.admission,
        proposal_id=value.get("proposal_id"),
        record_id=value.get("record_ref") or value.get("corrects_memory_id"),
        require_baseline=value["method"] == "memory.applyReviewed",
        _native_memory_operation=source_operation)
    # Numeric custody only. Preserve the SAME original references object;
    # no replay of its selectors or returned private row bodies.
    import asyncio
    frame = _native_memory_original_lookup_payer(db, source_operation)
    guard = db.info.get("composition_guard")
    if (guard.header_budget is not frame
            or db.sync_session.get_transaction() is not source_operation.transaction
            or not source_operation.driver.in_transaction):
        raise NativeServiceBlocked("native_memory_retention_original_source_changed")
    frame.debit(7 * 4 + 8, appearance=("memory-source-capacity", "original-lookup-lineage"))
    guard._native_memory_reference_lookup_original = (source_operation, frame,
        asyncio.current_task(), source_operation.transaction, source_operation.driver, references, source)
    return references


async def _memory_existing_reference_lookup(db, admission, *, proposal_id, record_id, require_baseline,
        _native_memory_operation=None):
    from src.db.models import (Goal, Memory, MemoryEntity, MemoryProposal, MemorySource,
        MemoryEdge, MemoryTombstone, WorkBoardProposal, WorkBoardDecisionReceipt, WorkBoardDecisionReceiptStage)
    from sqlalchemy import or_
    from src.memory.composition_headers import current_budget
    from src.memory.header_bounds import (
        _MEMORY_LEDGER_PHASE, _NativeMemoryNumericLedger,
        _native_memory_ascii_string_width, _native_memory_utf8_width)
    payer = (current_budget() if _native_memory_operation is None
        else _native_memory_original_lookup_payer(db, _native_memory_operation))
    active = _MEMORY_LEDGER_PHASE.get()
    if active is not None:
        ledger, phase = active
        if type(ledger) is not _NativeMemoryNumericLedger:
            raise NativeServiceBlocked("native_memory_retention_locator_unavailable")
        from src.workspace.accounting_witness import _native_memory_capacity_slots
        _native_memory_capacity_slots(ledger)
        if ledger.bindings[phase][0] is not db or (payer is not None and payer is not ledger.budget):
            raise NativeServiceBlocked("native_memory_retention_locator_unavailable")
        payer = ledger.budget
    def pay(stage, upper):
        if payer is not None:
            payer.debit(upper, appearance=("native-memory-reference-lookup", stage))
    def string_upper(value):
        return _native_memory_ascii_string_width(value)
    value = admission.candidate()
    pay("reference-set", 2)
    references = set()
    def add(table, key):
        if type(key) is not str or not key:
            raise NativeServiceBlocked("native_memory_retention_bound_exceeded")
        pay("reference-key-utf8", _native_memory_utf8_width(key))
        pay("reference-membership-tuple", 3 + string_upper(table) + string_upper(key))
        if (len(key.encode("utf-8")) > 512
                or ((table, key) not in references and len(references) >= 128)):
            raise NativeServiceBlocked("native_memory_retention_bound_exceeded")
        pay("reference-add", 3 + string_upper(table) + string_upper(key))
        references.add((table, key))
    if "source" in value:
        goal_id = value["source"]["goal_id"]
        pay("goal-seen-set", 2)
        seen = set()
        while goal_id is not None:
            if goal_id in seen:
                raise NativeServiceBlocked("native_memory_goal_ancestry_changed")
            pay("goal-seen-add", string_upper(goal_id))
            seen.add(goal_id)
            matches = await _memory_locator_rows(db, Goal,
                ("id", "parent_id", "owner_principal_id", "owner_session_id"), Goal.id == goal_id, limit=2, _native_memory_operation=_native_memory_operation)
            goal = matches[0] if len(matches) == 1 else None
            if (goal is None or goal["owner_principal_id"] != value["operator_principal_id"]
                or goal["owner_session_id"] != value["operator_session_id"]):
                raise NativeServiceBlocked("native_memory_goal_ancestry_changed")
            add("goals", goal["id"])
            goal_id = goal["parent_id"]
    pay("memory-id-set", 2)
    memory_ids = set()
    if proposal_id is not None:
        matches = await _memory_locator_rows(db, MemoryProposal,
            ("proposal_id", "owner_principal_id", "owner_session_id", "recovered_from_proposal_id",
             "proposal_job_id", "corrects_memory_id"), MemoryProposal.proposal_id == proposal_id, limit=2, _native_memory_operation=_native_memory_operation)
        proposal = matches[0] if len(matches) == 1 else None
        if (proposal is None or proposal["owner_principal_id"] != value["operator_principal_id"]
            or proposal["owner_session_id"] != value["operator_session_id"]):
            raise NativeServiceBlocked("native_memory_retention_proposal_changed")
        add("memory_proposals", proposal["proposal_id"])
        if proposal["recovered_from_proposal_id"] is not None:
            raise NativeServiceBlocked("native_memory_recovery_profile_unsupported")
        if proposal["proposal_job_id"] is not None:
            board = await _memory_locator_rows(db, WorkBoardProposal,
                ("proposal_id", "owner_principal_id", "owner_session_id"),
                WorkBoardProposal.admission_job_id == proposal["proposal_job_id"], limit=2, _native_memory_operation=_native_memory_operation)
            if (len(board) != 1 or board[0]["owner_principal_id"] != value["operator_principal_id"]
                or board[0]["owner_session_id"] != value["operator_session_id"]):
                raise NativeServiceBlocked("native_memory_retention_board_proposal_changed")
            add("work_board_proposals", board[0]["proposal_id"])
        baselines = await _memory_locator_rows(db, WorkBoardDecisionReceipt, ("receipt_id",),
            WorkBoardDecisionReceipt.source_proposal_id == proposal["proposal_id"],
            WorkBoardDecisionReceipt.receipt_stage == WorkBoardDecisionReceiptStage.source_baseline, limit=2, _native_memory_operation=_native_memory_operation)
        if require_baseline and len(baselines) != 1:
            raise NativeServiceBlocked("native_memory_retention_baseline_changed")
        if len(baselines) > 1:
            raise NativeServiceBlocked("native_memory_retention_baseline_changed")
        for row in baselines:
            add("work_board_decision_receipts", row["receipt_id"])
        if proposal["corrects_memory_id"]:
            pay("memory-id-add", string_upper(proposal["corrects_memory_id"]))
            memory_ids.add(proposal["corrects_memory_id"])
    if record_id is not None:
        pay("memory-id-add", string_upper(record_id))
        memory_ids.add(record_id)
    pay("memory-id-sort", 2 + sum(string_upper(key) + 1 for key in memory_ids))
    for key in sorted(memory_ids):
        matches = await _memory_locator_rows(db, Memory,
            ("id", "source_session_id", "subject_entity_id", "project_entity_id"), Memory.id == key, limit=2, _native_memory_operation=_native_memory_operation)
        record = matches[0] if len(matches) == 1 else None
        if record is None or record["source_session_id"] != value["operator_session_id"]:
            raise NativeServiceBlocked("native_memory_retention_record_changed")
        add("memories", record["id"])
        pay("entity-id-tuple", 3 + sum(4 if record[name] is None else string_upper(record[name])
            for name in ("subject_entity_id", "project_entity_id")))
        for entity_id in (record["subject_entity_id"], record["project_entity_id"]):
            if entity_id is not None:
                entities = await _memory_locator_rows(db, MemoryEntity, ("id",), MemoryEntity.id == entity_id, limit=2, _native_memory_operation=_native_memory_operation)
                if len(entities) != 1:
                    raise NativeServiceBlocked("native_memory_retention_entity_changed")
                add("memory_entities", entities[0]["id"])
        for cls, table in ((MemorySource, "memory_sources"), (MemoryTombstone, "memory_tombstones")):
            rows = await _memory_locator_rows(db, cls, ("id",), cls.memory_id == key, _native_memory_operation=_native_memory_operation)
            for row in rows:
                add(table, row["id"])
    if value["method"] == "memory.applyReviewed" and record_id is not None:
        edges = await _memory_locator_rows(db, MemoryEdge, ("id", "from_memory_id", "to_memory_id"), or_(
            MemoryEdge.from_memory_id == record_id, MemoryEdge.to_memory_id == record_id), _native_memory_operation=_native_memory_operation)
        for row in edges:
            if row["from_memory_id"] not in memory_ids or row["to_memory_id"] not in memory_ids:
                raise NativeServiceBlocked("native_memory_retention_edge_changed")
            add("memory_edges", row["id"])
    reference_upper = 2 + sum(4 + string_upper(table) + string_upper(key) for table, key in references)
    pay("reference-sort", reference_upper)
    pay("reference-dto", reference_upper)
    pay("reference-return", reference_upper)
    return tuple(NativeMemoryOwnerReference(table, key) for table, key in sorted(references))
