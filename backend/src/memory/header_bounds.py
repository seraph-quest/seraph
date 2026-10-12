"""Bounded SQLite row headers. Certificates are byte evidence, never authority.

The original caller owns the actual transaction, access policy and cumulative
whole-closure reservation. No body is selected before every requested header
has passed the conservative bound.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from types import MappingProxyType
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from weakref import WeakValueDictionary

from sqlalchemy import Boolean, Float, Integer, text
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

MAX_ROWS = 128
MAX_BYTES = 1_048_576
_SEAL = object()
_MEMORY_LEDGER_SEAL = object()
_MEMORY_LEDGERS = WeakValueDictionary()
_MEMORY_LEDGER_PHASE = ContextVar("native_memory_numeric_phase", default=None)
_MEMORY_CHARGE_TRACE = ContextVar("native_memory_charge_trace", default=None)


class HeaderBoundsError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, eq=False)
class ClosedRowDescriptor:
    table: str
    key: str
    columns: tuple[str, ...]
    kinds: tuple[str, ...]
    nullable: tuple[bool, ...]
    sql_types: tuple[str, ...]
    _seal: object = field(repr=False)


def _descriptor(model, key, columns=None):
    columns = tuple(columns or model.__table__.columns.keys())
    kinds, nullable = [], []
    for name in columns:
        column = model.__table__.columns[name]
        kinds.append("integer" if isinstance(column.type, (Integer, Boolean)) else
                     "real" if isinstance(column.type, Float) else "text")
        nullable.append(column.nullable)
    sql_types = tuple(model.__table__.columns[name].type.compile(dialect=sqlite_dialect()).upper()
                      for name in columns)
    return ClosedRowDescriptor(model.__tablename__, key, columns, tuple(kinds), tuple(nullable), sql_types, _SEAL)


from src.db import models as _models

_MEMORY_FIELDS = {'goals': ['id', 'parent_id', 'path', 'level', 'title', 'description', 'status', 'domain', 'start_date', 'due_date', 'sort_order', 'revision', 'success_criterion_json', 'proactive_enabled', 'owner_principal_id', 'owner_session_id', 'admission_budget_json', 'guardian_policy_json', 'guardian_policy_revision', 'goal_programmes_json', 'created_at', 'updated_at'], 'memories': ['id', 'content', 'category', 'kind', 'summary', 'confidence', 'importance', 'reinforcement', 'status', 'subject_entity_id', 'project_entity_id', 'source_session_id', 'embedding_id', 'scope_key', 'metadata_json', 'created_at', 'updated_at', 'last_confirmed_at'], 'memory_edges': ['id', 'from_memory_id', 'to_memory_id', 'edge_type', 'weight', 'metadata_json', 'created_at'], 'memory_entities': ['id', 'canonical_key', 'canonical_name', 'entity_type', 'aliases_json', 'created_at', 'updated_at'], 'memory_proposals': ['proposal_id', 'schema_version', 'owner_principal_id', 'owner_session_id', 'source_task_id', 'source_task_revision', 'source_attempt_id', 'source_attempt_fence', 'workflow_run_id', 'workflow_run_revision', 'goal_id', 'goal_revision', 'capability_id', 'capability_version', 'typed_input_digest', 'source_context_digest', 'candidate_set_digest', 'evidence_digest', 'readback_kind', 'readback_ref', 'readback_digest', 'artifact_ref', 'artifact_digest', 'proposal_job_id', 'request_idempotency_key', 'request_binding_digest', 'acceptance_binding_digest', 'memory_kind', 'memory_scope_json', 'preview_text', 'preview_text_digest', 'decision_effect', 'confidence', 'corrects_memory_id', 'recovered_from_proposal_id', 'provenance_json', 'source_refs_json', 'reason_code', 'recovery_action', 'provider_contact_started', 'provider_contact_state', 'provider_contact_count', 'privacy_state', 'status', 'accepted_memory_id', 'accepted_memory_content_digest', 'accepted_by_principal_id', 'accepted_by_session_id', 'accepted_at', 'rejected_by_principal_id', 'rejected_by_session_id', 'rejected_at', 'rollback_by_principal_id', 'rollback_by_session_id', 'rollback_at', 'rollback_reason', 'created_at', 'updated_at', 'expires_at', 'revision'], 'memory_sources': ['id', 'memory_id', 'source_type', 'source_session_id', 'source_message_id', 'snippet', 'created_at'], 'memory_tombstones': ['id', 'memory_id', 'actor', 'reason', 'created_at'], 'work_board_decision_receipts': ['receipt_id', 'schema_version', 'receipt_stage', 'receipt_binding_digest', 'receipt_integrity_mac', 'owner_principal_id', 'owner_session_id', 'source_proposal_id', 'source_proposal_revision', 'source_baseline_receipt_id', 'source_task_id', 'source_task_revision', 'source_attempt_id', 'source_attempt_fence', 'source_workflow_run_id', 'source_workflow_run_revision', 'later_task_id', 'later_task_revision', 'later_attempt_id', 'later_workflow_run_id', 'later_attempt_fence', 'goal_id', 'goal_revision', 'capability_id', 'capability_version', 'typed_input_digest', 'task_intent_digest', 'source_context_digest', 'candidate_set_digest', 'accepted_memory_id', 'accepted_memory_content_digest', 'before_input_digest', 'after_input_digest', 'before_action_id', 'after_action_id', 'before_selected_capability_id', 'after_selected_capability_id', 'confirmed_action_id', 'comparison_context_digest', 'retrieval_evidence_ids_json', 'decision_status', 'admission_status', 'reason', 'confirmer_principal_id', 'confirmer_session_id', 'confirmed_at', 'confirmation_binding_digest', 'consumed_at', 'created_at', 'updated_at', 'revision'], 'work_board_proposals': ['proposal_id', 'opportunity_id', 'opportunity_revision', 'owner_principal_id', 'owner_session_id', 'parent_task_id', 'parent_revision', 'goal_revision', 'kind', 'idempotency_key', 'request_digest', 'capability_id', 'capability_version', 'authority_digest', 'grant_revision', 'input_digest', 'route_id', 'admission_job_id', 'effect_id_digest', 'provider_contact_started', 'provider_contact_state', 'status', 'proposal_json', 'proposal_digest', 'evidence_use_snapshot_json', 'estimated_cost', 'created_at', 'expires_at', 'revision']}
_MEMORY_MODELS = {
    "goals": _models.Goal, "memories": _models.Memory,
    "memory_edges": _models.MemoryEdge, "memory_entities": _models.MemoryEntity,
    "memory_proposals": _models.MemoryProposal, "memory_sources": _models.MemorySource,
    "memory_tombstones": _models.MemoryTombstone,
    "work_board_decision_receipts": _models.WorkBoardDecisionReceipt,
    "work_board_proposals": _models.WorkBoardProposal,
}
_MEMORY_KEYS = {"memory_proposals": "proposal_id", "work_board_proposals": "proposal_id",
                "work_board_decision_receipts": "receipt_id"}
MEMORY_DESCRIPTORS = MappingProxyType({name: _descriptor(model, _MEMORY_KEYS.get(name, "id"), _MEMORY_FIELDS[name])
                                      for name, model in _MEMORY_MODELS.items()})
WRS_LEGACY_PARENT = _descriptor(_models.WorkflowRunState, "id")
WRS_BY_RUN = _descriptor(_models.WorkflowRunState, "run_identity")
WORKFLOW_STEP = _descriptor(_models.WorkflowStepState, "id")
WORKFLOW_ARTIFACT_REVIEW = _descriptor(_models.WorkflowArtifactReview, "id")
OPERATOR_SESSION = _descriptor(_models.OperatorSession, "id")
WORK_BOARD_TASK = _descriptor(_models.WorkBoardTask, "task_id")
WORK_BOARD_ATTEMPT = _descriptor(_models.WorkBoardAttempt, "attempt_id")
GOAL = MEMORY_DESCRIPTORS["goals"]
AUDIT_EVENT = _descriptor(_models.AuditEvent, "id")
_DESCRIPTORS = (*MEMORY_DESCRIPTORS.values(), WRS_LEGACY_PARENT, WRS_BY_RUN,
                WORKFLOW_STEP, WORKFLOW_ARTIFACT_REVIEW, OPERATOR_SESSION, AUDIT_EVENT,
                WORK_BOARD_TASK, WORK_BOARD_ATTEMPT)


_COMPOSITION_MODELS = {
    'messages': _models.Message,
    'work_board_tasks': _models.WorkBoardTask,
    'work_board_input_artifacts': _models.WorkBoardInputArtifact,
    'work_board_attempts': _models.WorkBoardAttempt,
    'work_board_review_intents': _models.WorkBoardReviewIntent,
    'work_board_links': _models.WorkBoardLink,
    'work_board_events': _models.WorkBoardEvent,
    'work_board_evidence_dependencies': _models.WorkBoardEvidenceDependency,
    'work_board_handoffs': _models.WorkBoardHandoff,
    'workflow_run_states': _models.WorkflowRunState,
    'workflow_step_states': _models.WorkflowStepState,
    'workflow_artifact_reviews': _models.WorkflowArtifactReview,
    'production_workflow_authority_states': _models.ProductionWorkflowAuthorityState,
    'production_workflow_fault_receipts': _models.ProductionWorkflowFaultReceipt,
    'production_workflow_side_effect_receipts': _models.ProductionWorkflowSideEffectReceipt,
    'runtime_composition_states': _models.RuntimeCompositionState,
    'sessions': _models.Session,
    'audit_events': _models.AuditEvent,
    'memory_episodes': _models.MemoryEpisode,
    'approval_requests': _models.ApprovalRequest,
    'goals': _models.Goal,
    'memories': _models.Memory,
    'memory_edges': _models.MemoryEdge,
    'memory_entities': _models.MemoryEntity,
    'memory_proposals': _models.MemoryProposal,
    'memory_sources': _models.MemorySource,
    'memory_tombstones': _models.MemoryTombstone,
    'work_board_decision_receipts': _models.WorkBoardDecisionReceipt,
    'work_board_proposals': _models.WorkBoardProposal,
    'inference_cost_reservations': _models.InferenceCostReservation,
    'operator_sessions': _models.OperatorSession,
    'secrets': _models.Secret,
    'inference_accounting_owners': _models.InferenceAccountingOwner,
}
_COMPOSITION_KEYS = {'messages': 'id', 'work_board_tasks': 'task_id', 'work_board_input_artifacts': 'artifact_id', 'work_board_attempts': 'attempt_id', 'work_board_review_intents': 'intent_id', 'work_board_links': 'link_id', 'work_board_events': 'event_id', 'work_board_evidence_dependencies': 'dependency_id', 'work_board_handoffs': 'handoff_id', 'workflow_run_states': 'run_identity', 'workflow_step_states': 'id', 'workflow_artifact_reviews': 'id', 'production_workflow_authority_states': 'id', 'production_workflow_fault_receipts': 'id', 'production_workflow_side_effect_receipts': 'id', 'runtime_composition_states': 'runtime_domain', 'sessions': 'id', 'audit_events': 'id', 'memory_episodes': 'id', 'approval_requests': 'id', 'goals': 'id', 'memories': 'id', 'memory_edges': 'id', 'memory_entities': 'id', 'memory_proposals': 'proposal_id', 'memory_sources': 'id', 'memory_tombstones': 'id', 'work_board_decision_receipts': 'receipt_id', 'work_board_proposals': 'proposal_id', 'inference_cost_reservations': 'operation_id', 'operator_sessions': 'id', 'secrets': 'id', 'inference_accounting_owners': 'id'}
COMPOSITION_DESCRIPTORS = MappingProxyType({name: next((d for d in _DESCRIPTORS if d.table == name and d.key == _COMPOSITION_KEYS[name]), _descriptor(model, _COMPOSITION_KEYS[name])) for name, model in _COMPOSITION_MODELS.items()})
SESSION = COMPOSITION_DESCRIPTORS["sessions"]
SECRET = COMPOSITION_DESCRIPTORS["secrets"]
INPUT_ARTIFACT = COMPOSITION_DESCRIPTORS["work_board_input_artifacts"]
RUNTIME_COMPOSITION = COMPOSITION_DESCRIPTORS["runtime_composition_states"]
_DESCRIPTORS = tuple(dict.fromkeys((*_DESCRIPTORS, *COMPOSITION_DESCRIPTORS.values())))


class HeaderReadBudget:
    """Shared byte evidence only; an original owner supplies all actual reads."""
    def __init__(self):
        self.remaining = MAX_BYTES
        self.references = set()
        self.physical_references = set()
        self.future_references = set()

    async def certify(self, db, descriptor, row_ids, *, _header_budget=None):
        if _header_budget is not None and _header_budget is not self:
            raise HeaderBoundsError("header_metadata_owner_unavailable")
        if not any(descriptor is item for item in _DESCRIPTORS):
            raise HeaderBoundsError("header_descriptor_unavailable")
        refs = {(descriptor.table, descriptor.key, identity) for identity in row_ids}
        appearance = ("exact-header", descriptor.table, descriptor.key, tuple(row_ids))
        certificate = await preflight_exact_rows(db, descriptor, tuple(row_ids), self.available(appearance),
            _header_budget=_header_budget)
        physical = {(descriptor.table, rowid) for rowid in certificate.rowids}
        for identity,rowid in zip(certificate.row_ids,certificate.rowids):
            self.resolve_future(descriptor,identity,rowid)
        self.enroll(physical)
        self.references |= refs
        self.debit(certificate.upper_bytes, appearance=appearance)
        await validate_certificate(db, certificate, _header_budget=_header_budget)
        return certificate

    def available(self, appearance=None):
        discovery = _DISCOVERY_NUMERIC_READS.get()
        if discovery is not None and discovery.budget is self:
            return self.remaining + discovery.available(appearance)
        session = _SESSION_NUMERIC_READS.get()
        if session is not None and session.budget is self:
            return self.remaining + session.available(appearance)
        active = _MEMORY_LEDGER_PHASE.get()
        if active is not None and active[0].budget is self:
            return self.remaining + active[0].available(active[1], appearance)
        return self.remaining

    def debit(self, amount, *, appearance=None):
        trace = _MEMORY_CHARGE_TRACE.get()
        if trace is not None and trace[0] is self:
            from src.memory.composition_headers import _CommonCertificationTraceCollector
            if type(trace[1]) is _CommonCertificationTraceCollector:
                trace[1].append_charge(appearance, amount)
            else:
                from src.runtime_plugins.memory_producer import _MemoryAuthorityTraceCollector
                if type(trace[1]) is _MemoryAuthorityTraceCollector:
                    trace[1].append_charge(appearance, amount)
                else:
                    from src.workflows.job_runtime import _NativeMemoryClaimTraceCollector
                    if type(trace[1]) is _NativeMemoryClaimTraceCollector:
                        trace[1].append_charge(appearance, amount)
                    else:
                        from src.workspace.accounting_witness import _NativeMemoryAccountingTraceCollector
                        if type(trace[1]) is _NativeMemoryAccountingTraceCollector:
                            trace[1].append_charge(appearance, amount)
                        else:
                            from src.runtime_plugins.memory_producer import _NativeMemoryReportTraceCollector
                            if type(trace[1]) is _NativeMemoryReportTraceCollector:
                                trace[1].append_charge(appearance, amount)
                            else:
                                from src.runtime_plugins.ownership import _NativeMemoryReportRuntimeTraceCollector
                                if type(trace[1]) is _NativeMemoryReportRuntimeTraceCollector:
                                    trace[1].append_charge(appearance, amount)
                                else:
                                    trace[1].append((appearance, amount))
        discovery = _DISCOVERY_NUMERIC_READS.get()
        if discovery is not None and discovery.budget is self:
            discovery.consume(appearance, amount)
            return
        session = _SESSION_NUMERIC_READS.get()
        if session is not None and session.budget is self:
            session.consume(appearance, amount)
            return
        active = _MEMORY_LEDGER_PHASE.get()
        if active is not None and active[0].budget is self and active[0].consume(active[1], appearance, amount):
            return
        if type(amount) is not int or amount < 0 or amount > self.remaining:
            raise HeaderBoundsError("canonical_bound_not_certified")
        self.remaining -= amount

    def enroll(self, physical):
        physical = set(physical)
        if len(self.physical_references | physical) + len(self.future_references) > MAX_ROWS:
            raise HeaderBoundsError("header_reference_bound")
        self.physical_references |= physical

    def reserve_future_row(self, descriptor, identity, upper_bytes, *, database_identity=None):
        """Numeric capacity for an actual not-yet-attached constructor address."""
        if not any(descriptor is item for item in _DESCRIPTORS):
            raise HeaderBoundsError("header_descriptor_unavailable")
        ref=(descriptor.table,descriptor.key,identity)
        if database_identity is not None:
            ref=(database_identity,*ref)
        if type(identity) is not str or not identity or len(identity.encode("utf-8"))>512:
            raise HeaderBoundsError("header_request_bound")
        if ref in self.future_references or ref in self.references:
            raise HeaderBoundsError("header_future_reference_duplicate")
        if len(self.physical_references)+len(self.future_references)+1>MAX_ROWS:
            raise HeaderBoundsError("header_reference_bound")
        self.debit(upper_bytes)
        self.future_references.add(ref)

    def _reserve_programme_identity(self, raw_owner, identity, upper_bytes):
        """Named original programme capacity only; no generic descriptor grant."""
        from src.workspace.accounting_continuity import _RawRollbackConnection
        if type(raw_owner) is not _RawRollbackConnection:
            raise HeaderBoundsError("header_raw_owner_unavailable")
        raw_owner._validate_budget(self)
        raw_owner._state(raw_owner._db)
        pair = raw_owner._pair
        if pair is None or raw_owner is not pair.destination or pair._writer is None:
            raise HeaderBoundsError("programme_copy_reservation_unavailable")
        _source, _destination, selection, compared = pair._writer
        source_identity, destination_identity, _source_rows, destination_rows = compared
        raw_owner._validate_programme_selection(selection)
        from src.memory.composition_headers import _validate
        if source_identity is None or destination_identity is None:
            raise HeaderBoundsError("programme_copy_reservation_unavailable")
        _validate(source_identity.connection, source_identity)
        _validate(destination_identity.connection, destination_identity)
        if (identity not in destination_identity.absent or destination_rows.get(identity, ()) is not None
                or upper_bytes != source_identity.rows.get(("operator_identities", identity), (None, None))[1]):
            raise HeaderBoundsError("programme_copy_reservation_unavailable")
        if type(identity) is not str or not 0 < len(identity.encode("utf-8")) <= 512:
            raise HeaderBoundsError("header_request_bound")
        ref = (raw_owner._namespace, "operator_identities", "id", identity)
        if ref in self.future_references or ref in self.references:
            raise HeaderBoundsError("header_future_reference_duplicate")
        if len(self.physical_references) + len(self.future_references) + 1 > MAX_ROWS:
            raise HeaderBoundsError("header_reference_bound")
        self.debit(upper_bytes, appearance=("programme-identity-prospective", identity))
        self.future_references.add(ref)

    def resolve_future(self, descriptor, identity, rowid, *, database_identity=None):
        active = _MEMORY_LEDGER_PHASE.get()
        if (active is not None and type(active[0]) is _NativeMemoryNumericLedger
                and active[0].budget is self):
            from src.workspace import accounting_witness
            resolve = getattr(accounting_witness, "_native_memory_resolve_numeric_slot", None)
            if resolve is not None:
                slot = resolve(active[0], descriptor, identity, rowid, database_identity)
                if slot is not None:
                    from src.workspace.accounting_witness import _NativeMemoryFutureIntegerSlot
                    physical = (descriptor.table, rowid)
                    if (type(slot) is not _NativeMemoryFutureIntegerSlot
                            or slot not in self.future_references or slot.budget is not self
                            or slot.descriptor is not descriptor or database_identity is not None
                            or type(rowid) is not int or rowid <= 0
                            or physical in self.physical_references):
                        raise HeaderBoundsError("memory_future_integer_slot_changed")
                    self.future_references.remove(slot)
                    self.physical_references.add(physical)
        ref=(descriptor.table,descriptor.key,identity)
        if database_identity is not None:
            ref=(database_identity,*ref)
        if ref in self.future_references:
            if type(rowid) is not int or rowid<=0:
                raise HeaderBoundsError("header_rowid_unavailable")
            self.future_references.remove(ref)
            self.physical_references.add((descriptor.table,rowid) if database_identity is None
                else (database_identity,descriptor.table,rowid))

    def _reserve_native_memory_integer_slot(self, ledger, slot):
        """Count the original sealed plan's liability without inventing an id."""
        import sys
        from src.workspace.accounting_witness import _NativeMemoryFutureIntegerSlot
        if (sys._getframe(1).f_code is not _reserve_native_memory_effect_ledger.__code__
                or type(ledger) is not _NativeMemoryNumericLedger
                or ledger.budget is not self or type(slot) is not _NativeMemoryFutureIntegerSlot):
            raise HeaderBoundsError("memory_future_integer_slot_changed")
        ledger._checked()
        slot._checked()
        cohort = slot.db.info["composition_guard"]._native_memory_integer_slots
        if (type(cohort) is not tuple or len(cohort) != 2 or cohort[0] is not ledger.operation
                or type(cohort[1]) is not tuple or not any(item is slot for item in cohort[1])
                or slot.operation is not ledger.operation or slot.prepared is not ledger.prepared
                or slot.budget is not self or slot.bound_key is not None
                or slot.expected_columns is None or slot in self.future_references):
            raise HeaderBoundsError("memory_future_integer_slot_changed")
        if len(self.physical_references) + len(self.future_references) + 1 > MAX_ROWS:
            raise HeaderBoundsError("header_reference_bound")
        # The complete ledger already paid all byte liability. This marker
        # affects only cardinality and is never a body or payload reference.
        self.future_references.add(slot)

    async def certify_all(self, db, descriptor, *, _header_budget=None):
        from src.memory.composition_headers import discover_rows
        ids = await discover_rows(db, descriptor, self, _header_budget=_header_budget)
        return await self.certify(db, descriptor, ids, _header_budget=_header_budget)


@contextmanager
def _trace_memory_numeric_charges(budget):
    """Record real original read mechanics; never enroll or grant a Source."""
    charges = []
    token = _MEMORY_CHARGE_TRACE.set((budget, charges))
    try:
        yield charges
    finally:
        _MEMORY_CHARGE_TRACE.reset(token)


def _native_memory_projected_header_upper(descriptor, identity, row):
    """The original full-header formula over resolved numeric-only binds."""
    return _native_memory_resolved_row_widths(descriptor, identity, row)[0]


def _native_memory_utf8_width(value):
    if type(value) is not str:
        raise HeaderBoundsError("memory_numeric_scalar_unavailable")
    total = 0
    for char in value:
        point = ord(char)
        if 0xD800 <= point <= 0xDFFF:
            raise HeaderBoundsError("memory_numeric_scalar_unavailable")
        total += 1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
    return total


def _native_memory_ascii_string_width(value):
    """Exact ensure_ascii string width without constructing JSON or UTF8."""
    _native_memory_utf8_width(value)
    return 2 + sum(2 if char in '"\\\b\f\n\r\t' else 6 if ord(char) < 32
        or 127 <= ord(char) <= 65535 else 12 if ord(char) > 65535 else 1 for char in value)


def _native_memory_tuple_skeleton_width(descriptor, identity):
    return _native_memory_tuple_skeleton_from_width(descriptor, _native_memory_ascii_string_width(identity))


def _native_memory_tuple_skeleton_from_width(descriptor, key_width):
    fields = 2 + max(0, len(descriptor.columns) - 1) + sum(
        7 + _native_memory_ascii_string_width(name) for name in descriptor.columns)
    return (5 + _native_memory_ascii_string_width("native-composition-memory.v1")
        + _native_memory_ascii_string_width(descriptor.table)
        + key_width + fields)


def _native_memory_integer_slot_row_widths(slot, row):
    """An issued plan's int64 capacity; no invented identity or body grant."""
    from src.workspace.accounting_witness import _NativeMemoryFutureIntegerSlot
    if type(slot) is not _NativeMemoryFutureIntegerSlot:
        raise HeaderBoundsError("memory_numeric_projection_identity_unassigned")
    slot._checked()
    descriptor = slot.descriptor
    if (descriptor is not COMPOSITION_DESCRIPTORS["work_board_events"]
            or slot.bound_key is not None or type(row) is not dict
            or len(row) != len(descriptor.columns) or slot.expected_columns is None
            or len(slot.expected_columns) != len(descriptor.columns) - 1
            or any(name not in row or row[name] != value for name, value in slot.expected_columns.items())
            or row.get(descriptor.key) is not None):
        raise HeaderBoundsError("memory_numeric_projection_identity_changed")
    # SQLite header locators retain the integer key; the Memory reference
    # codec retains its canonical decimal string, hence two quote bytes.
    header = _native_memory_tuple_skeleton_from_width(descriptor, 20) + 128
    raw = _native_memory_tuple_skeleton_from_width(descriptor, 22)
    for name, kind, nullable in zip(descriptor.columns, descriptor.kinds, descriptor.nullable):
        value = row[name]
        if name == descriptor.key:
            header += 20 - 4
            raw += 20 - 4
        elif value is None:
            if not nullable:
                raise HeaderBoundsError("header_scalar_unavailable")
        elif kind == "text" and type(value) is str:
            header += 6 * _native_memory_utf8_width(value) + 2 - 4
            raw += _native_memory_ascii_string_width(value) - 4
        elif kind == "integer" and type(value) is int and -(2**63) <= value < 2**63:
            header += 20 - 4
            raw += 20 - 4
        elif kind == "real" and type(value) is float and math.isfinite(value):
            header += 64 - 4
            raw += 128 - 4
        else:
            raise HeaderBoundsError("header_scalar_type_unsupported")
    if header > MAX_BYTES or raw > MAX_BYTES:
        raise HeaderBoundsError("canonical_bound_not_certified")
    return header, raw


def _native_memory_resolved_row_widths(descriptor, identity, row):
    """Capacity over actual resolved SQL values; never a row certificate.

    The header result preserves the original full-header fee exactly. The raw
    tuple result also covers the original finite float.hex representation.
    Neither computation serializes a tuple, skeleton or private field value.
    """
    if (not any(descriptor is item for item in COMPOSITION_DESCRIPTORS.values())
            or type(identity) is not str or not identity
            or _native_memory_utf8_width(identity) > 512
            or type(row) is not dict or len(row) != len(descriptor.columns)
            or any(name not in row for name in descriptor.columns)):
        raise HeaderBoundsError("memory_numeric_projection_identity_changed")
    skeleton = _native_memory_tuple_skeleton_width(descriptor, identity)
    amount, raw_tuple = skeleton + 128, skeleton
    for name, kind, nullable in zip(descriptor.columns, descriptor.kinds, descriptor.nullable):
        value = row[name]
        if value is None:
            if not nullable:
                raise HeaderBoundsError("header_scalar_unavailable")
        elif kind == "text" and type(value) is str:
            amount += 6 * _native_memory_utf8_width(value) + 2 - 4
            raw_tuple += _native_memory_ascii_string_width(value) - 4
        elif kind == "integer" and type(value) is int and -(2**63) <= value < 2**63:
            amount += 20 - 4
            raw_tuple += 20 - 4
        elif kind == "real" and type(value) is float and math.isfinite(value):
            amount += 64 - 4
            raw_tuple += 128 - 4
        else:
            raise HeaderBoundsError("header_scalar_type_unsupported")
    if amount > MAX_BYTES or raw_tuple > MAX_BYTES:
        raise HeaderBoundsError("canonical_bound_not_certified")
    return amount, raw_tuple


def _native_memory_array_width(widths):
    return 2 + sum(widths) + max(0, len(widths) - 1)


def _native_memory_object_width(fields):
    """Numeric JSON construction shape over the original literal field names."""
    return 2 + sum(_native_memory_ascii_string_width(name) + 1 + upper
        for name, upper in fields.items()) + max(0, len(fields) - 1)


def _native_memory_current_payload_upper(references, owner_events, *, method,
        unknown_reason=None, original_present=True):
    """Bound the original fourteen-field Current without constructing it.

    Encoded byte counts have at most seven decimal digits under MAX_BYTES.
    This bounds all thirty-two convergence attempts and the final build; it
    supplies neither their actual counts nor a receipt or body-read grant.
    """
    from src.workspace.accounting_witness import (
        MEMORY_REFERENCE_PROFILE, MEMORY_CURRENT_CHECKPOINT,
        _MEMORY_CURRENT_FIELDS, _MEMORY_UNKNOWN_REASONS, _MEMORY_NATIVE_OPERATIONS)
    if (type(references) is not tuple or type(owner_events) is not tuple
            or not 0 < len(references) <= MAX_ROWS or len(owner_events) > 2
            or type(method) is not str or method not in _MEMORY_NATIVE_OPERATIONS
            or type(original_present) is not bool
            or (unknown_reason is not None and (type(unknown_reason) is not str
                or unknown_reason not in _MEMORY_UNKNOWN_REASONS))):
        raise HeaderBoundsError("memory_numeric_reference_shape_invalid")
    def reference_width(ref):
        if (type(ref) is not tuple or len(ref) != 2 or type(ref[0]) is not str
                or type(ref[1]) is not str or ref[0] not in COMPOSITION_DESCRIPTORS
                or not ref[1] or _native_memory_utf8_width(ref[1]) > 512):
            raise HeaderBoundsError("memory_numeric_reference_shape_invalid")
        return _native_memory_object_width({"table": _native_memory_ascii_string_width(ref[0]),
            "key": _native_memory_ascii_string_width(ref[1])})
    refs = tuple(reference_width(ref) for ref in references)
    events = tuple(reference_width(ref) for ref in owner_events)
    if (references != tuple(sorted(set(references)))
            or owner_events != tuple(sorted(set(owner_events)))
            or any(ref not in references for ref in owner_events)):
        raise HeaderBoundsError("memory_numeric_reference_shape_invalid")
    witnesses = tuple(_native_memory_object_width({"ref": upper,
        "tuple_digest": 66, "encoded_bytes": 7}) for upper in refs)
    unknown = unknown_reason is not None
    fields = {"schema_version": 1,
        "profile": _native_memory_ascii_string_width(MEMORY_REFERENCE_PROFILE),
        "original_checkpoint_digest": 66 if original_present else 4,
        "projection_revision": 20,
        "state": _native_memory_ascii_string_width("unknown" if unknown else "validated"),
        "reason_code": _native_memory_ascii_string_width(unknown_reason) if unknown else 4,
        "refs": _native_memory_array_width(refs), "absences": 2,
        "rows": 2 if unknown else _native_memory_array_width(witnesses),
        "rows_digest": 4 if unknown else 66, "encoded_bytes": 7,
        "owner_operation_kind": _native_memory_ascii_string_width(method),
        "owner_events": 2 if unknown else _native_memory_array_width(events),
        "selected_delta_digest": 66}
    if fields.keys() != _MEMORY_CURRENT_FIELDS or (not unknown and not original_present):
        raise HeaderBoundsError("memory_numeric_reference_shape_invalid")
    payload = _native_memory_object_width(fields)
    record = _native_memory_object_width({
        "checkpoint_id": _native_memory_ascii_string_width(MEMORY_CURRENT_CHECKPOINT),
        "safe": 4, "state_digest": 66, "payload": payload})
    return payload, record


def _native_memory_future_wrs_widths(before_row, *, text_bytes, null_fields=(), integer_fields=()):
    """Future raw WRS widths from source-owned SQL-bind dimensions only.

    text_bytes measures logical UTF8 bytes, including complete future journal
    and context widths. This view constructs no future row, JSON or timestamp.
    It is capacity only; actual publication still requires exact raw readback.
    """
    descriptor = COMPOSITION_DESCRIPTORS["workflow_run_states"]
    if (type(before_row) is not dict or len(before_row) != len(descriptor.columns)
            or any(name not in before_row for name in descriptor.columns)
            or type(text_bytes) is not dict or type(null_fields) is not tuple
            or type(integer_fields) is not tuple):
        raise HeaderBoundsError("memory_numeric_wrs_shape_invalid")
    changed = (*text_bytes, *null_fields, *integer_fields)
    if (len(changed) != len(set(changed))
            or any(name not in descriptor.columns for name in changed)
            or any(type(size) is not int or not 0 <= size <= MAX_BYTES
                for size in text_bytes.values())):
        raise HeaderBoundsError("memory_numeric_wrs_shape_invalid")
    header, raw_tuple = _native_memory_resolved_row_widths(
        descriptor, before_row["run_identity"], before_row)
    for name, kind, nullable in zip(descriptor.columns, descriptor.kinds, descriptor.nullable):
        if name not in changed:
            continue
        value = before_row[name]
        old_header = (4 if value is None else 6 * _native_memory_utf8_width(value) + 2
            if kind == "text" else 20 if kind == "integer" else 64)
        old_tuple = (4 if value is None else _native_memory_ascii_string_width(value)
            if kind == "text" else 20 if kind == "integer" else 128)
        if name in text_bytes and kind == "text":
            upper = 6 * text_bytes[name] + 2
            header += upper - old_header
            raw_tuple += upper - old_tuple
        elif name in integer_fields and kind == "integer":
            header += 20 - old_header
            raw_tuple += 20 - old_tuple
        elif name in null_fields and nullable:
            header += 4 - old_header
            raw_tuple += 4 - old_tuple
        else:
            raise HeaderBoundsError("memory_numeric_wrs_shape_invalid")
    if header > MAX_BYTES or raw_tuple > MAX_BYTES:
        raise HeaderBoundsError("canonical_bound_not_certified")
    return header, raw_tuple


def _native_memory_projected_row_header_entries(descriptor, identity):
    """The five original _headers appearances for one future physical row."""
    from src.memory.composition_headers import _metadata_upper
    from src.workspace.accounting_witness import _NativeMemoryFutureIntegerSlot
    if COMPOSITION_DESCRIPTORS.get(descriptor.table) is not descriptor:
        raise HeaderBoundsError("memory_numeric_projection_identity_changed")
    if type(identity) is _NativeMemoryFutureIntegerSlot:
        identity._checked()
        if identity.descriptor is not descriptor:
            raise HeaderBoundsError("memory_numeric_projection_identity_changed")
        key_upper = 20
    elif descriptor.table == "work_board_events" and type(identity) is int:
        if not -(2**63) <= identity < 2**63:
            raise HeaderBoundsError("memory_numeric_projection_identity_changed")
        key_upper = 20
    elif type(identity) is str and 0 < _native_memory_utf8_width(identity) <= 512:
        key_upper = 6 * _native_memory_utf8_width(identity) + 2
    else:
        raise HeaderBoundsError("memory_numeric_projection_identity_unassigned")
    upper = _metadata_upper(2, (20, *([44, 20, 64] * len(descriptor.columns))))
    return tuple((("metadata-prequery", "row-header", stage), upper)
        for stage in ("delivery", "row-copy", "encoded-copy")) + (
        (("metadata-prequery", "row-header", "skeleton-copy"), key_upper
            + 6 * len(descriptor.table) + sum(6 * len(name) + 12 for name in descriptor.columns) + 256),
        (("metadata-prequery", "row-header", "header-fact-copy"),
            key_upper + 6 * len(descriptor.table) + 256))


def _native_memory_source_superset_charges(certificate, trace, new_rows, updated_rows, operation):
    """ONE original common certification over exact retained Source dimensions."""
    from src.workspace.accounting_witness import (
        _native_memory_generic_future_bound_inputs, _NativeMemoryFutureIntegerSlot)
    from src.runtime_plugins.memory_producer import _MemoryOwnerSourceOperation
    if type(operation) is not _MemoryOwnerSourceOperation:
        raise HeaderBoundsError("memory_future_header_source_unavailable")
    guard = operation.db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_source_capacity_facts", None)
    retention = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if retained is None or retention is None:
        raise HeaderBoundsError("memory_future_header_source_unavailable")
    facts, widths = retained[3], retention[3]
    guard, _rows, own, _audit, _new_addresses, _value = _native_memory_generic_future_bound_inputs(
        operation, operation._prepared, certificate, facts, widths)
    common = getattr(guard, "_native_memory_source_common_numeric", None)
    projection = guard._native_memory_source_projection
    if (common is None or common[0] is not operation or common[1] is not certificate
            or common[2] is not trace or type(trace) is not tuple
            or projection[1] is not new_rows or projection[2] is not updated_rows
            or certificate.metadata_budget is not guard.header_budget):
        raise HeaderBoundsError("memory_future_header_source_changed")
    headers = dict(certificate.rows)
    old_ids = {table: tuple(key for name, key in headers if name == table)
        for table in COMPOSITION_DESCRIPTORS}
    added = {table: [] for table in COMPOSITION_DESCRIPTORS}
    def physical_key(table, key):
        if table != "work_board_events" or type(key) is _NativeMemoryFutureIntegerSlot:
            return key
        try:
            value = int(key) if type(key) is str and key else None
        except ValueError:
            value = None
        if value is None or str(value) != key or not -(2**63) <= value < 2**63:
            raise HeaderBoundsError("memory_numeric_projection_identity_changed")
        return value
    for descriptor, key, _row in new_rows:
        identity = physical_key(descriptor.table, key)
        if ((descriptor.table, identity) in headers
                or any(value == 2**63 - 1 for (table, _key), (value, _upper) in certificate.rows.items()
                    if table == descriptor.table)):
            raise HeaderBoundsError("memory_future_header_locator_order_unavailable")
        if descriptor.table != "work_board_events" and added[descriptor.table]:
            # No source-owned flush-order fact exists for multiple text inserts.
            raise HeaderBoundsError("memory_future_header_locator_order_unavailable")
        upper = facts["row_uppers"][(descriptor.table, key)]
        headers[(descriptor.table, identity)] = (None, upper)
        added[descriptor.table].append(identity)
    for descriptor, key, _changes in updated_rows:
        identity = physical_key(descriptor.table, key)
        if (descriptor.table, identity) not in certificate.rows:
            raise HeaderBoundsError("memory_future_header_source_changed")
        headers[(descriptor.table, identity)] = (None, facts["row_uppers"][(descriptor.table, key)])
    if own not in certificate.rows:
        raise HeaderBoundsError("memory_future_header_source_changed")
    headers[own] = (None, widths["own_upper"])
    affected = getattr(guard, "_native_memory_affected_numeric_facts", None)
    if affected is not None:
        if (affected[0] is not operation or affected[1] is not operation._prepared
                or affected[2] is not certificate):
            raise HeaderBoundsError("memory_future_header_source_changed")
        for run_id, dimensions in affected[4].items():
            if ("workflow_run_states", run_id) not in certificate.rows:
                raise HeaderBoundsError("memory_future_header_source_changed")
            headers[("workflow_run_states", run_id)] = (None, dimensions["row_upper"])
    if len(headers) > MAX_ROWS:
        raise HeaderBoundsError("header_reference_bound")
    future_ids = {}
    for table, identities in old_ids.items():
        extra = added[table]
        if table == "work_board_events":
            assigned = sorted((*identities, *(key for key in extra if type(key) is int)))
            slots = tuple(key for key in extra if type(key) is _NativeMemoryFutureIntegerSlot)
            if ((slots and any(type(key) is int for key in extra))
                    or tuple(slot.ordinal for slot in slots) != tuple(sorted(slot.ordinal for slot in slots))):
                raise HeaderBoundsError("memory_future_header_locator_order_unavailable")
            future_ids[table] = (*assigned, *slots)
        else:
            future_ids[table] = (*identities, *extra)
    result, current_table, seen = [], None, []
    stages = ("delivery", "row-copy", "encoded-copy")
    for appearance, amount in trace:
        if (type(appearance) is not tuple or not appearance
                or type(amount) is not int or not 0 <= amount <= MAX_BYTES):
            raise HeaderBoundsError("memory_numeric_projection_trace_invalid")
        if len(appearance) == 3 and appearance[:2] == ("metadata-prequery", "descriptor-compiler"):
            current_table = appearance[2]
            if current_table not in COMPOSITION_DESCRIPTORS:
                raise HeaderBoundsError("memory_numeric_projection_trace_invalid")
        if (current_table is not None and appearance[:2] == ("metadata-prequery", "row-locator")
                and appearance[-1] in stages):
            extra = added[current_table]
            # Original selected-whole SQL:90+escaped text(orNULL)+integer(orNULL).
            amount += sum(114 if type(key) in (int, _NativeMemoryFutureIntegerSlot)
                else 96 + 6 * _native_memory_utf8_width(key) for key in extra)
            amount += max(0, len(future_ids[current_table]) - 1) - max(0, len(old_ids[current_table]) - 1)
        if appearance[0] == "complete-headers":
            _, table, identities = appearance
            if table != current_table or identities != old_ids[table] or table in seen:
                raise HeaderBoundsError("memory_numeric_projection_trace_invalid")
            for identity in added[table]:
                result.extend(_native_memory_projected_row_header_entries(COMPOSITION_DESCRIPTORS[table], identity))
            amount = sum(headers[(table, key)][1] for key in future_ids[table])
            appearance = ("complete-headers", table, future_ids[table])
            seen.append(table)
        if appearance[0] == "table-body":
            _, table, identities = appearance
            if identities != old_ids[table]:
                raise HeaderBoundsError("memory_numeric_projection_trace_invalid")
            amount = sum(headers[(table, key)][1] for key in future_ids[table])
            appearance = ("table-body", table, future_ids[table])
        if amount > MAX_BYTES:
            raise HeaderBoundsError("canonical_bound_not_certified")
        result.append((appearance, amount))
    if tuple(seen) != tuple(COMPOSITION_DESCRIPTORS):
        raise HeaderBoundsError("memory_future_header_complete_trace_unavailable")
    return tuple(result), headers


def _native_memory_projected_superset_charges(certificate, trace, new_rows, updated_rows, *, source_operation=None):
    """Forecast original complete-header appearances, never a certificate.

    This view does no SQL and grants no body read. Actual post-effect reads
    still require their fresh original full33 certificate. A privately issued
    unassigned integer slot is retained only as numerical capacity, never
    promoted to a reference address or a future body certificate.
    """
    from src.memory.composition_headers import CompositionHeaderCertificate, _validate, _metadata_upper
    from src.workspace.accounting_witness import _NativeMemoryFutureIntegerSlot
    if type(certificate) is not CompositionHeaderCertificate:
        raise HeaderBoundsError("header_certificate_unavailable")
    _validate(certificate.connection, certificate)
    if source_operation is not None:
        return _native_memory_source_superset_charges(certificate, trace, new_rows, updated_rows, source_operation)
    headers = dict(certificate.rows)
    old_ids = {table: tuple(key for name, key in headers if name == table)
        for table in COMPOSITION_DESCRIPTORS}
    new_ids = {table: [] for table in COMPOSITION_DESCRIPTORS}
    selected = set()
    for creating, values in ((True, new_rows), (False, updated_rows)):
        for descriptor, identity, row in values:
            is_slot = type(identity) is _NativeMemoryFutureIntegerSlot
            if is_slot and (not creating or identity.descriptor is not descriptor):
                raise HeaderBoundsError("memory_numeric_projection_identity_changed")
            ref = (descriptor.table, identity)
            if ref in selected or (creating == (ref in headers)):
                raise HeaderBoundsError("memory_numeric_projection_identity_changed")
            selected.add(ref)
            upper = (_native_memory_integer_slot_row_widths(identity, row)[0] if is_slot
                else _native_memory_projected_header_upper(descriptor, identity, row))
            headers[ref] = (None, upper)
            if creating:
                new_ids[descriptor.table].append(identity)
    if len(headers) > MAX_ROWS:
        raise HeaderBoundsError("header_reference_bound")
    result = []
    for appearance, amount in trace:
        if type(appearance) is not tuple or type(amount) is not int or not 0 <= amount <= MAX_BYTES:
            raise HeaderBoundsError("memory_numeric_projection_trace_invalid")
        if appearance[0] == "locator-metadata":
            _, table, key, tombstone, identities = appearance
            if key is not None or tombstone or identities != old_ids[table]:
                raise HeaderBoundsError("memory_numeric_projection_trace_invalid")
            identities = (*identities, *new_ids[table])
            # Actual rowid aliases are unknown until the original flush. Only
            # their finite int64 byte bound participates in this resource view.
            for identity in new_ids[table]:
                if type(identity) is _NativeMemoryFutureIntegerSlot:
                    amount += _metadata_upper(1, (20, 9, 20, 4, 20))
                elif type(identity) is str:
                    amount += _metadata_upper(1, (20, 6, 20,
                        _native_memory_ascii_string_width(identity), 4))
                else:
                    raise HeaderBoundsError("memory_numeric_projection_identity_unassigned")
            appearance = ("locator-metadata", table, None, False, tuple(identities))
        elif appearance[0] == "complete-headers":
            _, table, identities = appearance
            if identities != old_ids[table]:
                raise HeaderBoundsError("memory_numeric_projection_trace_invalid")
            identities = (*identities, *new_ids[table])
            amount = sum(headers[(table, identity)][1] for identity in identities)
            appearance = ("complete-headers", table, tuple(identities))
        result.append((appearance, amount))
    return tuple(result), headers


_DISCOVERY_NUMERIC_READS = ContextVar("original_discovery_numeric_reads", default=None)


class _DiscoveryNumericReservation:
    """Paid appearances for one genuine original selected-candidate continuation."""
    def __init__(self, guard, discovery_session, constructor, entries):
        import inspect
        from src.workspace.accounting_witness import CompositionSessionGuard
        if (inspect.currentframe().f_back.f_code not in {
                CompositionSessionGuard._reserve_original_discovery_admission.__code__,
                CompositionSessionGuard._reserve_original_discovery_queue.__code__}
                or type(guard) is not CompositionSessionGuard
                or _DISCOVERY_NUMERIC_READS.get() is not None
                or _SESSION_NUMERIC_READS.get() is not None
                or _MEMORY_LEDGER_PHASE.get() is not None):
            raise HeaderBoundsError("programme_numeric_reservation_unavailable")
        discovery_session.validate(db=guard.db, header_budget=guard.header_budget)
        self.guard, self.owner = guard, discovery_session
        self.budget, self.constructor = guard.header_budget, constructor
        self.candidate = discovery_session._candidate
        self.issued_id, self.closed, self.active_phase = id(self), False, None
        if type(entries) is not tuple:
            raise HeaderBoundsError("programme_numeric_schedule_invalid")
        total = 0
        for phase, appearance, upper in entries:
            if (phase not in {"staging", "admission", "queue", "publication-admission", "publication-queue"}
                    or (appearance is not None and type(appearance) is not tuple)
                    or type(upper) is not int or not 0 <= upper <= MAX_BYTES):
                raise HeaderBoundsError("programme_numeric_schedule_invalid")
            total += upper
        from src.workspace.production import _publication_json_upper
        # Check the complete numerical map and all future appearances before
        # allocating the retained map. Neither failed fit nor unused entries
        # refund the original frame.
        # The consumed log retains phase/appearance/max/actual for each real
        # appearance. It needs one further int64 field and separator per entry
        # beyond the schedule's triples; all of it is prepaid as well.
        storage_upper = 2 * _publication_json_upper(entries) + 21 * len(entries) + 4
        self.budget.debit(total + storage_upper,
            appearance=("discovery-full-continuation-reservation", constructor["job_id"]))
        self.entries, self.consumed = {}, []
        for phase, appearance, upper in entries:
            self.entries.setdefault((phase, appearance), []).append(upper)

    def _checked(self):
        self.owner.validate(db=self.guard.db, header_budget=self.budget)
        if (type(self) is not _DiscoveryNumericReservation or self.issued_id != id(self)
                or self.closed or self.guard._discovery_reservation is not self
                or self.owner._candidate is not self.candidate
                or self.owner._constructor_bounds is not self.constructor):
            raise HeaderBoundsError("programme_numeric_reservation_owner_changed")

    def available(self, appearance):
        self._checked()
        pending = self.entries.get((self.active_phase, appearance))
        return max(pending) if pending else 0

    def consume(self, appearance, amount):
        self._checked()
        if (type(appearance) is tuple and len(appearance) == 4
                and appearance[0] == "discovery-staging-reservation"):
            import inspect
            from src.work_board.research_artifacts import _DiscoveryStagingReservation, discovery_prefix
            caller = inspect.currentframe().f_back.f_back
            stager = caller.f_locals.get("self")
            if (caller.f_code is not _DiscoveryStagingReservation.__init__.__code__
                    or type(stager) is not _DiscoveryStagingReservation
                    or self.active_phase != "staging"
                    or stager.programme_id != self.candidate[1].programme_id
                    or stager.kind not in {"public_brief", "plan"}
                    or stager.reference != appearance[1] or stager.digest != appearance[2]
                    or stager.size != appearance[3]
                    or not stager.reference.startswith(discovery_prefix(stager.programme_id))
                    or stager.size > self.constructor[
                        "brief_bytes" if stager.kind == "public_brief" else "plan_bytes"]):
                raise HeaderBoundsError("programme_numeric_stager_changed")
            appearance = ("discovery-staging-reservation", stager.kind)
        pending = self.entries.get((self.active_phase, appearance))
        if (type(amount) is not int or amount < 0 or not pending or amount > max(pending)):
            raise HeaderBoundsError("programme_numeric_appearance_unreserved")
        # Current and future owner traces can share a metadata appearance while
        # its genuine finite width grows. Consume one sufficient reservation;
        # every unused or excess byte remains spent in the original frame.
        index = min((index for index, upper in enumerate(pending) if upper >= amount),
            key=lambda index: pending[index])
        maximum = pending.pop(index)
        self.consumed.append((self.active_phase, appearance, maximum, amount))

    @contextmanager
    def phase(self, phase):
        self._checked()
        existing = _DISCOVERY_NUMERIC_READS.get()
        if phase in {"publication-admission", "publication-queue"}:
            from src.db.engine import _get_original_discovery_exit_completion
            _get_original_discovery_exit_completion(self.owner,
                phase.removeprefix("publication-"), self.candidate[0])
        if existing is not None:
            writer_phase = phase.removeprefix("publication-")
            if (existing is not self or phase not in {"publication-admission", "publication-queue"}
                    or self.active_phase != writer_phase):
                raise HeaderBoundsError("programme_numeric_phase_unavailable")
        if (phase not in {"staging", "admission", "queue", "publication-admission", "publication-queue"}
                or _SESSION_NUMERIC_READS.get() is not None or _MEMORY_LEDGER_PHASE.get() is not None):
            raise HeaderBoundsError("programme_numeric_phase_unavailable")
        previous, self.active_phase = self.active_phase, phase
        token = _DISCOVERY_NUMERIC_READS.set(self)
        try:
            yield
        finally:
            _DISCOVERY_NUMERIC_READS.reset(token)
            self.active_phase = previous

    def close(self):
        self.closed = True


def _discovery_projected_superset_charges(certificate, trace, *, job_id, row_upper):
    """Numeric common33 appearances for ONE original future discovery address.

    No speculative row/certificate/body is constructed. The actual constructor
    binds, fresh certificate and current owner are independently mandatory.
    This helper neither reserves capacity nor authorizes any SQL/file effect.
    """
    from src.memory.composition_headers import CompositionHeaderCertificate, _validate, _metadata_cost, _metadata_upper
    if type(certificate) is not CompositionHeaderCertificate:
        raise HeaderBoundsError("header_certificate_unavailable")
    _validate(certificate.connection, certificate)
    if (type(job_id) is not str or not job_id.startswith("goal-discovery:")
            or len(job_id) != len("goal-discovery:") + 32
            or any(char not in "0123456789abcdef" for char in job_id[len("goal-discovery:"):])
            or type(row_upper) is not int or not 0 < row_upper <= MAX_BYTES
            or (WRS_BY_RUN.table, job_id) in certificate.rows
            or len(certificate.rows) + 1 > MAX_ROWS):
        raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
    old_ids = tuple(key for table, key in certificate.rows if table == WRS_BY_RUN.table)
    if any(rowid == 2**63 - 1 for (table, _key), (rowid, _upper) in certificate.rows.items()
            if table == WRS_BY_RUN.table):
        # SQLite cannot promise the appended physical locator at its int64
        # ceiling. Do not stage against an unknown future locator ordering.
        raise HeaderBoundsError("programme_constructor_rowid_bound")
    new_ids = (*old_ids, job_id)
    entries = []
    current_descriptor = None
    for appearance, amount in trace:
        if ((appearance is not None and type(appearance) is not tuple)
                or type(amount) is not int or not 0 <= amount <= MAX_BYTES):
            raise HeaderBoundsError("programme_numeric_trace_changed")
        if (type(appearance) is tuple and len(appearance) == 3
                and appearance[:2] == ("metadata-prequery", "descriptor-compiler")):
            current_descriptor = appearance[2]
        if (current_descriptor == WRS_BY_RUN.table and type(appearance) is tuple
                and appearance[:2] == ("metadata-prequery", "row-locator")
                and appearance[-1] in {"delivery", "row-copy", "encoded-copy"}):
            # Original selected-whole locator arithmetic (90 + escaped text
            # width + NULL integer alias), plus its actual new separator.
            amount += 90 + 6 * len(job_id) + 2 + 4 + int(bool(old_ids))
        if type(appearance) is tuple and appearance[:2] == ("locator-metadata", WRS_BY_RUN.table):
            if appearance != ("locator-metadata", WRS_BY_RUN.table, None, False, old_ids):
                raise HeaderBoundsError("programme_numeric_trace_changed")
            # Generated physical rowid is not an authority locator. Only its
            # original finite int64 width is forecast; actual order is checked.
            amount += _metadata_cost([[2**63 - 1, "text", len(job_id), job_id, None]])
            appearance = ("locator-metadata", WRS_BY_RUN.table, None, False, new_ids)
        elif type(appearance) is tuple and appearance[:2] == ("complete-headers", WRS_BY_RUN.table):
            if appearance != ("complete-headers", WRS_BY_RUN.table, old_ids):
                raise HeaderBoundsError("programme_numeric_trace_changed")
            amount += row_upper
            appearance = ("complete-headers", WRS_BY_RUN.table, new_ids)
            if certificate.metadata_budget is not None:
                # Exactly one future physical row adds the original _headers
                # query's three output appearances and two stored copies.
                header_upper = _metadata_upper(2, (20, *([44, 20, 64] * len(WRS_BY_RUN.columns))))
                for stage in ("delivery", "row-copy", "encoded-copy"):
                    entries.append((("metadata-prequery", "row-header", stage), header_upper))
                entries.append((("metadata-prequery", "row-header", "skeleton-copy"),
                    6 * len(job_id) + 2 + 6 * len(WRS_BY_RUN.table)
                    + sum(6 * len(name) + 12 for name in WRS_BY_RUN.columns) + 256))
                entries.append((("metadata-prequery", "row-header", "header-fact-copy"),
                    6 * len(job_id) + 2 + 6 * len(WRS_BY_RUN.table) + 256))
        elif type(appearance) is tuple and appearance[:2] == ("table-body", WRS_BY_RUN.table):
            if appearance != ("table-body", WRS_BY_RUN.table, old_ids):
                raise HeaderBoundsError("programme_numeric_trace_changed")
            amount += row_upper
            appearance = ("table-body", WRS_BY_RUN.table, new_ids)
        entries.append((appearance, amount))
    return tuple(entries)


@dataclass(eq=False)
class _NativeMemoryNumericLedger:
    budget: HeaderReadBudget
    operation: object
    prepared: object
    source: object
    entries: dict
    seal: object
    bindings: dict = field(default_factory=dict)
    consumed: list = field(default_factory=list)
    closed: bool = False
    _complete_owner: object = field(default=None, repr=False)
    _completion_capacity_task: object = field(default=None, repr=False)

    def _checked(self):
        import asyncio
        if (self.closed or self.seal is not _MEMORY_LEDGER_SEAL
                or _MEMORY_LEDGERS.get(id(self)) is not self
                or self.operation._prepared is not self.prepared
                or self.operation.source is not self.source
                or self.source.admission.header_budget is not self.budget
                or not self.source._live.is_set()
                or (self._completion_capacity_task is not None
                    and self._completion_capacity_task is not asyncio.current_task())):
            raise HeaderBoundsError("memory_numeric_ledger_unavailable")

    @asynccontextmanager
    async def source_phase(self, source, phase):
        """Completion capacity before its writer; this grants no body access."""
        import asyncio
        from src.runtime_plugins.memory_producer import _validate_memory_completion_capacity_source
        self._checked()
        if (source is not self.source or phase != "completion"
                or self._complete_owner is not _MEMORY_LEDGER_SEAL
                or self._completion_capacity_task is not None
                or _MEMORY_LEDGER_PHASE.get() is not None
                or _DISCOVERY_NUMERIC_READS.get() is not None
                or _SESSION_NUMERIC_READS.get() is not None):
            raise HeaderBoundsError("memory_numeric_ledger_phase_invalid")
        current = _validate_memory_completion_capacity_source(source, source.claim)
        if current is not source or source._retention.ledger is not self:
            raise HeaderBoundsError("memory_numeric_ledger_unavailable")
        self._completion_capacity_task = asyncio.current_task()
        token = _MEMORY_LEDGER_PHASE.set((self, phase))
        try:
            yield self
        finally:
            # Completion may already be durable here. Cleanup cannot reassert
            # an uncompleted state or erase the genuine publication binding.
            _MEMORY_LEDGER_PHASE.reset(token)
            self._completion_capacity_task = None

    def available(self, phase, appearance):
        self._checked()
        if type(self) is _NativeMemoryNumericLedger:
            from src.workspace import accounting_witness
            normalize = getattr(accounting_witness, "_native_memory_normalize_numeric_appearance", None)
            if normalize is not None:
                appearance = normalize(self, appearance)
        pending = self.entries.get((phase, appearance), ())
        return pending[0] if pending else 0

    def consume(self, phase, appearance, amount):
        self._checked()
        if type(self) is _NativeMemoryNumericLedger:
            from src.workspace import accounting_witness
            normalize = getattr(accounting_witness, "_native_memory_normalize_numeric_appearance", None)
            if normalize is not None:
                appearance = normalize(self, appearance)
        if type(amount) is not int or amount < 0:
            raise HeaderBoundsError("memory_numeric_ledger_charge_invalid")
        pending = self.entries.get((phase, appearance))
        if not pending:
            if type(self) is _NativeMemoryNumericLedger and self._complete_owner is _MEMORY_LEDGER_SEAL:
                raise HeaderBoundsError("memory_numeric_appearance_unreserved")
            return False
        maximum = pending[0]
        if amount > maximum:
            raise HeaderBoundsError("memory_numeric_ledger_charge_exceeded")
        # The full maximum was deducted at issuance. Never refund its slack.
        del pending[0]
        self.consumed.append((phase, appearance, maximum, amount))
        return True

    async def bind(self, db, phase, *, certificate=None):
        import asyncio
        self._checked()
        if self._complete_owner is _MEMORY_LEDGER_SEAL and phase == "completion":
            from sqlalchemy.ext.asyncio import AsyncSession
            from src.workspace.accounting_witness import CompositionSessionGuard
            if type(db) is not AsyncSession:
                raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
            guard = db.info.get("composition_guard")
            prior = self.bindings.get(phase)
            capacity = (self._completion_capacity_task is asyncio.current_task()
                and _MEMORY_LEDGER_PHASE.get() == (self, "completion"))
            if (type(guard) is not CompositionSessionGuard or guard.db is not db
                    or guard.header_budget is not self.budget
                    or (not capacity and (prior is None or prior[0] is not db
                        or prior[5] is not asyncio.current_task()))):
                raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
        transaction, driver, changes = await _connection_state(db)
        if phase not in {"effect", "capture", "completion"}:
            raise HeaderBoundsError("memory_numeric_ledger_phase_invalid")
        prior = self.bindings.get(phase)
        completion_capacity = (phase == "completion"
            and self._completion_capacity_task is asyncio.current_task()
            and _MEMORY_LEDGER_PHASE.get() == (self, "completion"))
        if self._complete_owner is _MEMORY_LEDGER_SEAL and phase == "completion":
            from src.workspace.accounting_witness import CompositionSessionGuard
            guard = db.info.get("composition_guard")
            if (type(guard) is not CompositionSessionGuard or guard.db is not db
                    or guard.header_budget is not self.budget
                    or (not completion_capacity and (prior is None
                        or prior[0] is not db or prior[1] is not transaction
                        or prior[2] is not driver or prior[5] is not asyncio.current_task()))):
                raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
        if prior is not None and not completion_capacity and (prior[0] is not db or prior[1] is not transaction or prior[2] is not driver):
            raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
        if phase != "completion" and (db is not self.operation.db
                or transaction is not self.operation.transaction or driver is not self.operation.driver):
            raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
        if certificate is not None:
            from src.memory.composition_headers import validate_composition_certificate
            await validate_composition_certificate(db, certificate)
            if certificate.budget is not self.budget:
                raise HeaderBoundsError("memory_numeric_ledger_certificate_changed")
        self.bindings[phase] = (db, transaction, driver, changes, certificate, asyncio.current_task())

    @contextmanager
    def phase(self, phase):
        import asyncio
        self._checked()
        binding = self.bindings.get(phase)
        if (binding is None or binding[0].sync_session.get_transaction() is not binding[1]
                or not binding[2].in_transaction or binding[5] is not asyncio.current_task()):
            raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
        active = _MEMORY_LEDGER_PHASE.get()
        if self._complete_owner is _MEMORY_LEDGER_SEAL and active is not None and not (phase == "completion" and active == (self, phase)
                and self._completion_capacity_task is asyncio.current_task()):
            raise HeaderBoundsError("memory_numeric_ledger_phase_invalid")
        token = _MEMORY_LEDGER_PHASE.set((self, phase))
        try:
            yield
        finally:
            _MEMORY_LEDGER_PHASE.reset(token)

    def close(self):
        self.closed = True
        _MEMORY_LEDGERS.pop(id(self), None)


async def _reserve_native_memory_numeric_ledger(db, run, operation, prepared, appearances):
    """Actual Source/SAME plan enrollment; serializable data never enrolls it."""
    from src.runtime_plugins.memory_producer import _validate_memory_owner_source
    from src.memory.m5 import validate_native_memory_mutation_plan
    source = await _validate_memory_owner_source(db, run, operation)
    await validate_native_memory_mutation_plan(prepared, db=db, candidate_digest=source.admission.candidate_digest)
    if operation._prepared is not prepared or type(source.admission.header_budget) is not HeaderReadBudget:
        raise HeaderBoundsError("memory_numeric_ledger_plan_changed")
    entries, total = {}, 0
    for phase, appearance, amount in appearances:
        if (phase not in {"effect", "capture", "completion"}
                or (appearance is not None and type(appearance) is not tuple)
                or type(amount) is not int or not 0 <= amount <= MAX_BYTES):
            raise HeaderBoundsError("memory_numeric_ledger_entry_invalid")
        entries.setdefault((phase, appearance), []).append(amount)
        total += amount
    source.admission.header_budget.debit(total)
    ledger = _NativeMemoryNumericLedger(source.admission.header_budget, operation, prepared, source,
        entries, _MEMORY_LEDGER_SEAL)
    _MEMORY_LEDGERS[id(ledger)] = ledger
    await ledger.bind(db, "effect")
    return ledger


async def _reserve_native_memory_effect_ledger(db, run, operation, prepared, *,
        existing_references, before_row, certificate):
    """Enroll only the complete original owner's pre-effect numeric forecast.

    Current certificates and numeric row sizes are capacity facts. They do not
    replace the live Source or the identical original, unconsumed M5 plan.
    An unavailable source-owned schedule stops before the first effect; it must
    never become a partial ledger or a caller-supplied list of fees.
    """
    from src.runtime_plugins.memory_producer import _validate_memory_owner_source
    from src.memory.m5 import validate_native_memory_mutation_plan
    from src.memory.composition_headers import (
        CompositionHeaderCertificate, validate_composition_certificate)
    from src.workspace import accounting_witness
    source = await _validate_memory_owner_source(db, run, operation)
    budget = source.admission.header_budget
    await validate_native_memory_mutation_plan(prepared, db=db,
        candidate_digest=source.admission.candidate_digest)
    if (operation._prepared is not prepared or type(budget) is not HeaderReadBudget
            or type(certificate) is not CompositionHeaderCertificate
            or certificate.budget is not budget
            or type(before_row) is not dict or set(before_row) != set(WRS_BY_RUN.columns)
            or before_row["run_identity"] != run.run_identity
            or type(existing_references) is not tuple
            or not 0 < len(existing_references) <= MAX_ROWS
            or any(type(ref) is not tuple or len(ref) != 2
                or type(ref[0]) is not str or type(ref[1]) is not str
                for ref in existing_references)
            or existing_references != tuple(sorted(set(existing_references)))
            or (WRS_BY_RUN.table, run.run_identity) not in existing_references
            or any(ref not in certificate.rows for ref in existing_references)):
        raise HeaderBoundsError("memory_numeric_forecast_owner_changed")
    await validate_composition_certificate(db, certificate)
    schedule_owner = getattr(accounting_witness, "_native_memory_effect_numeric_schedule", None)
    if schedule_owner is None:
        raise HeaderBoundsError("memory_complete_numeric_forecast_unavailable")
    appearances = await schedule_owner(db, run, operation, prepared,
        existing_references=existing_references, before_row=before_row, certificate=certificate)
    if (type(appearances) is not tuple or not appearances
            or any(type(entry) is not tuple or len(entry) != 3 for entry in appearances)
            or {entry[0] for entry in appearances} != {"effect", "capture", "completion"}):
        raise HeaderBoundsError("memory_complete_numeric_forecast_unavailable")
    # The original ledger checks every amount and deducts the complete sum
    # atomically before installing any prepaid effect phase. No slack refund.
    ledger = await _reserve_native_memory_numeric_ledger(db, run, operation, prepared, appearances)
    try:
        cohort = db.info["composition_guard"]._native_memory_integer_slots
        if type(cohort) is not tuple or len(cohort) != 2 or cohort[0] is not operation or type(cohort[1]) is not tuple:
            raise HeaderBoundsError("memory_future_integer_slot_changed")
        for slot in cohort[1]:
            budget._reserve_native_memory_integer_slot(ledger, slot)
        ledger._complete_owner = _MEMORY_LEDGER_SEAL
    except BaseException:
        ledger.close()
        raise
    return ledger


@dataclass(eq=False)
class _NativeProgrammeNumericLedger:
    """The same numeric debit protocol, owned by the actual programme Source."""
    budget: HeaderReadBudget
    guard: object
    source: object
    called: object
    run: object
    entries: dict
    seal: object
    bindings: dict = field(default_factory=dict)
    consumed: list = field(default_factory=list)
    closed: bool = False
    _delivered_capacity_task: object = field(default=None, repr=False)

    def _checked(self):
        from src.runtime_plugins.programme_producer import _ProgrammeSource
        from src.workspace.accounting_witness import CompositionSessionGuard
        if (type(self) is not _NativeProgrammeNumericLedger or self.closed
                or self.seal is not _MEMORY_LEDGER_SEAL
                or _MEMORY_LEDGERS.get(id(self)) is not self
                or type(self.source) is not _ProgrammeSource
                or getattr(self.source, "_numeric_reservation", None) is not self
                or type(self.guard) is not CompositionSessionGuard
                or self.guard.header_budget is not self.budget
                or self.source.header_budget is not self.budget
                or self.source.job_id != self.run.run_identity):
            raise HeaderBoundsError("programme_start_numeric_owner_changed")
        self.source.validate()
        active = _MEMORY_LEDGER_PHASE.get()
        if active is not None and active[0] is self and active[1] == "delivered":
            import asyncio
            self.source.validate(owner=True)
            if self._delivered_capacity_task is not asyncio.current_task():
                raise HeaderBoundsError("programme_start_numeric_owner_changed")

    available = _NativeMemoryNumericLedger.available
    close = _NativeMemoryNumericLedger.close

    def consume(self, phase, appearance, amount):
        if not _NativeMemoryNumericLedger.consume(self, phase, appearance, amount):
            raise HeaderBoundsError("programme_start_numeric_appearance_unreserved")
        return True

    def _checked_delivery(self):
        from src.runtime_plugins.programme_producer import _StartPublication
        self._checked()
        self.source.validate(owner=True)
        proof = self.source.publication
        response = getattr(self.source, "_delivered_response", None)
        if (type(proof) is not _StartPublication or proof.source is not self.source
                or proof.called is not self.called or not proof.checked or not proof.committed
                or self.source.state != "start_committed" or response is None
                or response is not self.source.response or response != proof.response):
            raise HeaderBoundsError("programme_start_numeric_delivery_unavailable")

    @asynccontextmanager
    async def source_phase(self, source, phase):
        """Capacity for actual delivery; each reader still establishes body authority."""
        import asyncio
        self._checked_delivery()
        if (source is not self.source or phase != "delivered"
                or self._delivered_capacity_task is not None
                or _MEMORY_LEDGER_PHASE.get() is not None
                or _DISCOVERY_NUMERIC_READS.get() is not None
                or _SESSION_NUMERIC_READS.get() is not None):
            raise HeaderBoundsError("programme_start_numeric_phase_invalid")
        self._delivered_capacity_task = asyncio.current_task()
        token = _MEMORY_LEDGER_PHASE.set((self, phase))
        try:
            yield self
        finally:
            _MEMORY_LEDGER_PHASE.reset(token)
            self._delivered_capacity_task = None
            self.bindings.pop("delivered", None)

    async def bind(self, db, phase):
        import asyncio
        from src.runtime_plugins.programme_producer import _StartPublication
        from src.workspace.accounting_witness import CompositionSessionGuard
        self._checked()
        if phase not in {"start", "publication", "delivered"}:
            raise HeaderBoundsError("programme_start_numeric_phase_invalid")
        held = self.source.writer
        if (type(held) is not tuple or len(held) != 6
                or held[0] is not asyncio.current_task() or held[1] is not db
                or type(held[3]) is not CompositionSessionGuard
                or db.info.get("composition_guard") is not held[3]
                or held[3].db is not db or held[3].header_budget is not self.budget):
            raise HeaderBoundsError("programme_start_numeric_owner_changed")
        current_guard = held[3]
        if phase == "delivered":
            self._checked_delivery()
            if (self._delivered_capacity_task is not asyncio.current_task()
                    or _MEMORY_LEDGER_PHASE.get() != (self, "delivered")):
                raise HeaderBoundsError("programme_start_numeric_phase_invalid")
        else:
            self.source.validate(called=self.called)
            if current_guard is not self.guard:
                raise HeaderBoundsError("programme_start_numeric_writer_changed")
            if phase == "publication":
                proof = self.source.publication
                if (type(proof) is not _StartPublication or proof.source is not self.source
                        or proof.called is not self.called or proof.db is not db
                        or proof.task is not asyncio.current_task() or not proof.checked):
                    raise HeaderBoundsError("programme_start_numeric_publication_unavailable")
        connection = held[2].sync_connection
        transaction = db.sync_session.get_transaction()
        sql_transaction = connection.get_transaction()
        driver = connection.connection.driver_connection
        if transaction is None or sql_transaction is None or not driver.in_transaction:
            raise HeaderBoundsError("programme_start_numeric_writer_changed")
        prior = self.bindings.get(phase)
        if phase != "delivered" and prior is not None and (prior[0] is not db or prior[1] is not transaction
                or prior[2] is not driver or prior[3] is not current_guard
                or prior[4] is not connection or prior[5] is not sql_transaction):
            raise HeaderBoundsError("programme_start_numeric_writer_changed")
        self.bindings[phase] = (db, transaction, driver, current_guard, connection,
            sql_transaction, asyncio.current_task())

    @contextmanager
    def phase(self, phase):
        import asyncio
        self._checked()
        binding = self.bindings.get(phase)
        held = self.source.writer
        if (binding is None or binding[0].sync_session.get_transaction() is not binding[1]
                or not binding[2].in_transaction
                or binding[4].get_transaction() is not binding[5]
                or binding[4].connection.driver_connection is not binding[2]
                or binding[6] is not asyncio.current_task()
                or held is None or held[0] is not binding[6] or held[1] is not binding[0]
                or held[2].sync_connection is not binding[4] or held[3] is not binding[3]):
            raise HeaderBoundsError("programme_start_numeric_writer_changed")
        if phase == "delivered":
            self._checked_delivery()
        else:
            self.source.validate(called=self.called)
        active = _MEMORY_LEDGER_PHASE.get()
        if active is not None and not (phase == "delivered" and active == (self, phase)
                and self._delivered_capacity_task is asyncio.current_task()):
            raise HeaderBoundsError("programme_start_numeric_phase_invalid")
        token = _MEMORY_LEDGER_PHASE.set((self, phase))
        try:
            yield self
        finally:
            _MEMORY_LEDGER_PHASE.reset(token)


def original_native_programme_delivered_numeric_schedule(source, called, *, future_row_upper, future_text_bytes):
    """Project the original physical-reader terms of delivered revalidation.

    The exact original trace supplies occurrence counts, including repeated
    immutable reads. Header/Identity/native-authority terms remain the original
    guard composer's responsibility; this helper neither enrolls nor grants.
    Unsupported physical derivation graphs stop before START.
    """
    from src.runtime_plugins.programme_producer import _ProgrammeSource
    from src.workflows.research_sources import _discovery_physical_function_entries
    if type(source) is not _ProgrammeSource:
        raise HeaderBoundsError("programme_start_numeric_owner_changed")
    source.validate(called=called)
    held = source._physical_numeric_owner
    facts, trace = source._physical_numeric_facts, source._physical_numeric_trace
    if (source.state != "claimed" or source.publication is not None or source.writer is None
            or type(held) is not tuple or len(held) != 5
            or held[0] is not source or held[1] is not source.claim
            or held[2] is not source.scope or held[3] is not source.header_budget
            or held[4] is not called or source._service_call is None
            or source._service_call[0] is not called
            or type(facts) is not MappingProxyType or type(trace) is not tuple
            or type(future_row_upper) is not int or not 0 <= future_row_upper <= MAX_BYTES
            or type(future_text_bytes) is not MappingProxyType):
        raise HeaderBoundsError("programme_start_physical_numeric_facts_changed")
    source.authority_selection(source.writer[1])
    widths = dict(facts["widths"])
    for name, amount in future_text_bytes.items():
        if type(name) is not str or type(amount) is not int or not 0 <= amount <= MAX_BYTES:
            raise HeaderBoundsError("programme_start_physical_numeric_facts_changed")
        widths[name] = amount
    widths["row_header_bytes"] = future_row_upper
    bounds = {}
    for function in ("original-rows", "records", "references", "provenance", "input-reader"):
        for appearance, amount in _discovery_physical_function_entries(function,
                job_id=source.job_id, widths=widths, count=facts["count"],
                plan_bytes=facts["plan_bytes"], brief_bytes=facts["brief_bytes"]):
            bounds[appearance] = max(amount, bounds.get(appearance, 0))
    entries = []
    for appearance, amount in trace:
        if type(amount) is not int or not 0 <= amount <= MAX_BYTES:
            raise HeaderBoundsError("programme_start_physical_numeric_facts_changed")
        if appearance is None:
            # These actual immutable file reads retain their original sizes.
            entries.append(("delivered", None, amount))
        elif type(appearance) is not tuple or not appearance:
            raise HeaderBoundsError("programme_start_physical_numeric_facts_changed")
        elif appearance[0] == "discovery-physical":
            if appearance not in bounds:
                raise HeaderBoundsError("programme_start_physical_graph_unavailable")
            entries.append(("delivered", appearance, max(amount, bounds[appearance])))
        elif appearance[0] == "native-programme-physical":
            if appearance not in {("native-programme-physical", source.job_id, "authority-model"),
                    ("native-programme-physical", source.job_id, "row-copy")}:
                raise HeaderBoundsError("programme_start_physical_graph_unavailable")
            entries.append(("delivered", appearance, max(amount, future_row_upper)))
    if not entries:
        raise HeaderBoundsError("programme_start_physical_numeric_facts_changed")
    return tuple(entries)


async def _reserve_native_programme_start_numeric_ledger(guard, source, called, run, entries):
    """Enroll only the exact complete vector retained by the original guard."""
    from src.runtime_plugins.programme_producer import _ProgrammeSource
    from src.workspace.accounting_witness import CompositionSessionGuard
    if type(guard) is not CompositionSessionGuard or type(source) is not _ProgrammeSource:
        raise HeaderBoundsError("programme_start_numeric_owner_changed")
    source.validate(called=called)
    owner = getattr(guard, "_native_programme_start_numeric_owner", None)
    if (type(owner) is not tuple or len(owner) != 3
            or owner[0] is not source or owner[1] is not called or owner[2] is not run
            or entries is not getattr(guard, "_native_programme_start_numeric_entries", None)
            or type(entries) is not tuple or not entries
            or source.authority_selection(guard.db)[0] is not guard
            or type(source.header_budget) is not HeaderReadBudget
            or guard.header_budget is not source.header_budget
            or source.state != "claimed" or source.publication is not None
            or getattr(source, "_numeric_reservation", None) is not None
            or run.run_identity != source.job_id):
        raise HeaderBoundsError("programme_start_numeric_owner_changed")
    projected, total, phases = {}, 0, set()
    for entry in entries:
        if type(entry) is not tuple or len(entry) != 3:
            raise HeaderBoundsError("programme_start_numeric_entry_invalid")
        phase, appearance, amount = entry
        if (phase not in {"start", "publication", "delivered"}
                or (appearance is not None and type(appearance) is not tuple)
                or type(amount) is not int or not 0 <= amount <= MAX_BYTES):
            raise HeaderBoundsError("programme_start_numeric_entry_invalid")
        phases.add(phase)
        projected.setdefault((phase, appearance), []).append(amount)
        total += amount
    if phases != {"start", "publication", "delivered"}:
        raise HeaderBoundsError("programme_start_complete_numeric_forecast_unavailable")
    source.header_budget.debit(total)
    ledger = _NativeProgrammeNumericLedger(source.header_budget, guard, source, called, run,
        projected, _MEMORY_LEDGER_SEAL)
    source._numeric_reservation = ledger
    _MEMORY_LEDGERS[id(ledger)] = ledger
    try:
        await ledger.bind(guard.db, "start")
    except BaseException:
        ledger.close()
        raise
    return ledger


_SESSION_NUMERIC_READS = ContextVar("original_session_numeric_reads", default=None)


class _SessionNumericReservation:
    """One original guard's prepaid read appearances; never row authority."""
    def __init__(self, guard, binding, entries):
        self.guard, self.binding, self.budget = guard, binding, guard.header_budget
        self.entries = {}
        self.consumed = []
        self.closed = False
        total = 0
        for appearance, amount in entries:
            if type(amount) is not int or not 0 <= amount <= MAX_BYTES:
                raise HeaderBoundsError("session_reservation_cost_invalid")
            self.entries.setdefault(appearance, []).append(amount)
            total += amount
        self.budget.debit(total, appearance=("session-publication-reservation",))

    def _checked(self):
        if (self.closed or self.guard._session_reservation is not self
                or self.guard._native_writer_fields(self.binding[5], self.budget, "finite_service") != self.binding):
            raise HeaderBoundsError("session_reservation_owner_changed")

    def available(self, appearance):
        self._checked()
        pending = self.entries.get(appearance, ())
        return pending[0] if pending else 0

    def consume(self, appearance, amount):
        self._checked()
        pending = self.entries.get(appearance)
        if not pending or type(amount) is not int or not 0 <= amount <= pending[0]:
            raise HeaderBoundsError("session_reservation_appearance_changed")
        self.consumed.append((appearance, pending.pop(0), amount))

    @contextmanager
    def reads(self):
        self._checked()
        if _SESSION_NUMERIC_READS.get() is not None:
            raise HeaderBoundsError("session_reservation_nested")
        token = _SESSION_NUMERIC_READS.set(self)
        try:
            yield
        finally:
            _SESSION_NUMERIC_READS.reset(token)

    def close(self):
        self.closed = True


@dataclass(frozen=True, eq=False)
class HeaderCertificate:
    descriptor: ClosedRowDescriptor
    row_ids: tuple[str | int, ...]
    rowids: tuple[int, ...]
    row_upper_bytes: tuple[int, ...]
    upper_bytes: int
    _db: object = field(repr=False)
    _transaction: object = field(repr=False)
    _driver: object = field(repr=False)
    _changes: int = field(repr=False)
    _seal: object = field(repr=False)
    _issued_id: int = field(default=0, repr=False)


async def _connection_state(db):
    if not db.in_transaction():
        raise HeaderBoundsError("header_transaction_required")
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
    if not driver.in_transaction:
        raise HeaderBoundsError("header_sqlite_transaction_required")
    transaction = db.sync_session.get_transaction()
    changes = await db.scalar(text("SELECT total_changes()"))
    return transaction, driver, changes


async def validate_certificate(db, certificate, *, _header_budget=None):
    if (type(certificate) is not HeaderCertificate or certificate._seal is not _SEAL
            or certificate._issued_id != id(certificate) or certificate._db is not db):
        raise HeaderBoundsError("header_certificate_unavailable")
    if _header_budget is not None:
        _header_budget.debit(20, appearance=("exact-metadata", "validation-state"))
    transaction, driver, changes = await _connection_state(db)
    if (transaction is not certificate._transaction or driver is not certificate._driver
            or changes != certificate._changes):
        raise HeaderBoundsError("header_certificate_stale")


def _quoted(identifier):
    # Identifiers come only from the private singleton descriptor registry.
    return '"' + identifier + '"'


async def preflight_exact_rows(db, descriptor, exact_row_ids, remaining_bytes, *, _header_budget=None):
    if not any(descriptor is item for item in _DESCRIPTORS):
        raise HeaderBoundsError("header_descriptor_unavailable")
    event_identity = descriptor is COMPOSITION_DESCRIPTORS["work_board_events"] and descriptor.key == "event_id"
    if (type(exact_row_ids) is not tuple or len(exact_row_ids) > MAX_ROWS
            or any((type(value) is not int or not -(2**63) <= value < 2**63)
                   if event_identity else (type(value) is not str or not value)
                   for value in exact_row_ids)
            or type(remaining_bytes) is not int or not 0 <= remaining_bytes <= MAX_BYTES):
        raise HeaderBoundsError("header_request_bound")
    try:
        valid_ids = (len(set(exact_row_ids)) == len(exact_row_ids)
                     and (event_identity or all(len(value.encode("utf-8")) <= 512 for value in exact_row_ids)))
    except UnicodeEncodeError:
        valid_ids = False
    if not valid_ids:
        raise HeaderBoundsError("header_request_bound")
    if _header_budget is not None:
        _header_budget.debit(20, appearance=("exact-metadata", "initial-state"))
    transaction, driver, changes = await _connection_state(db)
    from src.memory.composition_headers import validate_descriptor_schema, _metadata_upper
    schema_cost = await (await db.connection()).run_sync(lambda c: validate_descriptor_schema(c, descriptor,
        _header_budget=_header_budget))
    if _header_budget is not None:
        _header_budget.debit(770, appearance=("exact-metadata", "sqlite-version"))
    version = await db.scalar(text("SELECT sqlite_version()"))
    if tuple(map(int, version.split("."))) < (3, 43, 0):
        raise HeaderBoundsError("header_sqlite_version_unsupported")
    if _header_budget is not None:
        _header_budget.debit(32, appearance=("exact-metadata", "encoding"))
    if await db.scalar(text("PRAGMA encoding")) != "UTF-8":
        raise HeaderBoundsError("header_encoding_unsupported")
    # The virtual pragma returns only bounded schema metadata, never defaults.
    if _header_budget is not None:
        for appearance in ("delivery", "row-copy", "encoded-copy"):
            _header_budget.debit(_metadata_upper(len(descriptor.columns) + 1, (770, 770, 20, 20, 20)),
                appearance=("exact-metadata", "schema", descriptor.table, appearance))
    schema = list(await db.execute(text(
        "SELECT CASE WHEN typeof(name)='text' THEN CASE WHEN octet_length(name)<=128 THEN name END END, "
        "CASE WHEN typeof(type)='text' THEN CASE WHEN octet_length(type)<=128 THEN type END END, "
        "\"notnull\",pk,hidden "
        "FROM pragma_table_xinfo(:table) LIMIT :limit"),
        {"table": descriptor.table, "limit": len(descriptor.columns)+1}))
    names = [row[0] for row in schema]
    if (len(schema) != len(descriptor.columns) or set(names) != set(descriptor.columns)
            or any(name in {"rowid", "_rowid_", "oid"} for name in names)):
        raise HeaderBoundsError("header_schema_changed")
    expected = dict(zip(descriptor.columns, zip(descriptor.sql_types, descriptor.nullable)))
    for name, sql_type, notnull, pk, hidden in schema:
        if (type(sql_type) is not str or type(notnull) is not int or type(pk) is not int
                or hidden != 0 or sql_type.upper() != expected[name][0] or notnull != int(not expected[name][1])):
            raise HeaderBoundsError("header_schema_changed")
    query_fields = ["_rowid_"]
    for name in descriptor.columns:
        column = _quoted(name)
        query_fields.extend((f"typeof({column})", f"octet_length({column})",
                             f"CASE WHEN typeof({column}) IN ('integer','real') THEN {column} ELSE NULL END"))
    statement = text("SELECT " + ",".join(query_fields) + " FROM " + _quoted(descriptor.table)
                     + " WHERE " + _quoted(descriptor.key) + " COLLATE BINARY=:key LIMIT 2")
    rowids, costs = [], []
    total = schema_cost
    for identity in exact_row_ids:
        if _header_budget is not None:
            for appearance in ("delivery", "row-copy", "encoded-copy"):
                _header_budget.debit(_metadata_upper(2, (20, *([44, 20, 64] * len(descriptor.columns)))),
                    appearance=("exact-metadata", "row-header", descriptor.table, identity, appearance))
        rows = list(await db.execute(statement, {"key": identity}))
        if len(rows) != 1:
            raise HeaderBoundsError("header_row_unavailable")
        row = rows[0]
        if (type(row[0]) is not int or (event_identity and (
                row[0] != identity or row[1 + 3 * descriptor.columns.index("event_id")] != "integer"
                or type(row[3 + 3 * descriptor.columns.index("event_id")]) is not int
                or row[3 + 3 * descriptor.columns.index("event_id")] != identity))):
            raise HeaderBoundsError("header_rowid_unavailable")
        # This exact skeleton covers field/key/tuple punctuation. Every text
        # value can require at most six JSON bytes per original UTF-8 byte.
        skeleton = ["native-composition-memory.v1", descriptor.table, identity,
                    [[name, None] for name in descriptor.columns]]
        cost = len(json.dumps(skeleton, ensure_ascii=True, separators=(",", ":")).encode()) + 128
        for index, (kind, nullable) in enumerate(zip(descriptor.kinds, descriptor.nullable)):
            storage, size, number = row[1+index*3:4+index*3]
            if storage == "null":
                if not nullable:
                    raise HeaderBoundsError("header_scalar_unavailable")
                continue
            if storage != kind:
                raise HeaderBoundsError("header_scalar_type_unsupported")
            if kind == "text":
                if type(size) is not int or size < 0:
                    raise HeaderBoundsError("header_scalar_unavailable")
                cost += 6*size + 2 - 4
            elif kind == "integer":
                if type(number) is not int or not -(2**63) <= number < 2**63:
                    raise HeaderBoundsError("header_scalar_unavailable")
                cost += 20 - 4
            else:
                if type(number) is not float or not math.isfinite(number):
                    raise HeaderBoundsError("header_scalar_unavailable")
                # Covers existing finite float64.hex tagged scalar encoding.
                cost += 64 - 4
        total += cost
        if total > remaining_bytes:
            raise HeaderBoundsError("canonical_bound_not_certified")
        rowids.append(row[0])
        costs.append(cost)
    # All headers fit before any private tuple/body SELECT is permitted.
    if _header_budget is not None:
        _header_budget.debit(20, appearance=("exact-metadata", "final-state"))
    if await db.scalar(text("SELECT total_changes()")) != changes:
        raise HeaderBoundsError("header_snapshot_changed")
    certificate = HeaderCertificate(descriptor, exact_row_ids, tuple(rowids), tuple(costs), total,
                                    db, transaction, driver, changes, _SEAL)
    object.__setattr__(certificate, "_issued_id", id(certificate))
    return certificate


def strict_json_loads(value, *, max_utf8_bytes=MAX_BYTES):
    if (type(value) is not str or type(max_utf8_bytes) is not int
            or not 0 <= max_utf8_bytes <= MAX_BYTES):
        raise HeaderBoundsError("header_json_bound")
    try:
        if len(value.encode("utf-8")) > max_utf8_bytes:
            raise HeaderBoundsError("header_json_bound")
    except UnicodeEncodeError as error:
        raise HeaderBoundsError("header_json_bound") from error
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise HeaderBoundsError("header_json_invalid")
            result[key] = item
        return result
    def invalid(_value):
        raise HeaderBoundsError("header_json_invalid")
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise HeaderBoundsError("header_json_invalid")
        return number
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        raise HeaderBoundsError("header_json_invalid") from error
