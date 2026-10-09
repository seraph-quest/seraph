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
from contextlib import contextmanager
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

    async def certify(self, db, descriptor, row_ids):
        if not any(descriptor is item for item in _DESCRIPTORS):
            raise HeaderBoundsError("header_descriptor_unavailable")
        refs = {(descriptor.table, descriptor.key, identity) for identity in row_ids}
        appearance = ("exact-header", descriptor.table, descriptor.key, tuple(row_ids))
        certificate = await preflight_exact_rows(db, descriptor, tuple(row_ids), self.available(appearance))
        physical = {(descriptor.table, rowid) for rowid in certificate.rowids}
        for identity,rowid in zip(certificate.row_ids,certificate.rowids):
            self.resolve_future(descriptor,identity,rowid)
        self.enroll(physical)
        self.references |= refs
        self.debit(certificate.upper_bytes, appearance=appearance)
        await validate_certificate(db, certificate)
        return certificate

    def available(self, appearance=None):
        active = _MEMORY_LEDGER_PHASE.get()
        if active is not None and active[0].budget is self:
            return self.remaining + active[0].available(active[1], appearance)
        return self.remaining

    def debit(self, amount, *, appearance=None):
        trace = _MEMORY_CHARGE_TRACE.get()
        if trace is not None and trace[0] is self:
            trace[1].append((appearance, amount))
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
        ref=(descriptor.table,descriptor.key,identity)
        if database_identity is not None:
            ref=(database_identity,*ref)
        if ref in self.future_references:
            if type(rowid) is not int or rowid<=0:
                raise HeaderBoundsError("header_rowid_unavailable")
            self.future_references.remove(ref)
            self.physical_references.add((descriptor.table,rowid) if database_identity is None
                else (database_identity,descriptor.table,rowid))

    async def certify_all(self, db, descriptor):
        from src.memory.composition_headers import discover_rows
        ids = await discover_rows(db, descriptor, self)
        return await self.certify(db, descriptor, ids)


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
    from src.workspace.accounting_witness import _native_memory_row_bytes
    if not any(descriptor is item for item in COMPOSITION_DESCRIPTORS.values()):
        raise HeaderBoundsError("header_descriptor_unavailable")
    _native_memory_row_bytes(descriptor, identity, row)
    skeleton = ["native-composition-memory.v1", descriptor.table, identity,
        [[name, None] for name in descriptor.columns]]
    amount = len(json.dumps(skeleton, ensure_ascii=True, separators=(",", ":")).encode()) + 128
    for name, kind, nullable in zip(descriptor.columns, descriptor.kinds, descriptor.nullable):
        value = row[name]
        if value is None:
            if not nullable:
                raise HeaderBoundsError("header_scalar_unavailable")
        elif kind == "text":
            amount += 6 * len(value.encode("utf-8")) + 2 - 4
        elif kind == "integer":
            amount += 20 - 4
        elif kind == "real":
            amount += 64 - 4
        else:
            raise HeaderBoundsError("header_scalar_type_unsupported")
    if amount > MAX_BYTES:
        raise HeaderBoundsError("canonical_bound_not_certified")
    return amount


def _native_memory_projected_superset_charges(certificate, trace, new_rows, updated_rows):
    """Forecast original complete-header appearances, never a certificate.

    This view does no SQL and grants no body read. Actual post-effect reads
    still require their fresh original full33 certificate. Constructor ids
    here must already have been assigned by the original sealed M5 plan;
    an unassigned numeric slot is never promoted to a reference address.
    """
    from src.memory.composition_headers import CompositionHeaderCertificate, _validate, _metadata_cost
    if type(certificate) is not CompositionHeaderCertificate:
        raise HeaderBoundsError("header_certificate_unavailable")
    _validate(certificate.connection, certificate)
    headers = dict(certificate.rows)
    old_ids = {table: tuple(key for name, key in headers if name == table)
        for table in COMPOSITION_DESCRIPTORS}
    new_ids = {table: [] for table in COMPOSITION_DESCRIPTORS}
    selected = set()
    for creating, values in ((True, new_rows), (False, updated_rows)):
        for descriptor, identity, row in values:
            ref = (descriptor.table, identity)
            if ref in selected or (creating == (ref in headers)):
                raise HeaderBoundsError("memory_numeric_projection_identity_changed")
            selected.add(ref)
            headers[ref] = (None, _native_memory_projected_header_upper(descriptor, identity, row))
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
                if type(identity) is not str:
                    raise HeaderBoundsError("memory_numeric_projection_identity_unassigned")
                amount += _metadata_cost([[2**63 - 1, "text", len(identity.encode("utf-8")), identity, None]])
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

    def _checked(self):
        if (self.closed or self.seal is not _MEMORY_LEDGER_SEAL
                or _MEMORY_LEDGERS.get(id(self)) is not self
                or self.operation._prepared is not self.prepared
                or self.operation.source is not self.source
                or self.source.admission.header_budget is not self.budget
                or not self.source._live.is_set()):
            raise HeaderBoundsError("memory_numeric_ledger_unavailable")

    def available(self, phase, appearance):
        self._checked()
        pending = self.entries.get((phase, appearance), ())
        return pending[0] if pending else 0

    def consume(self, phase, appearance, amount):
        self._checked()
        if type(amount) is not int or amount < 0:
            raise HeaderBoundsError("memory_numeric_ledger_charge_invalid")
        pending = self.entries.get((phase, appearance))
        if not pending:
            return False
        maximum = pending[0]
        if amount > maximum:
            raise HeaderBoundsError("memory_numeric_ledger_charge_exceeded")
        # The full maximum was deducted at issuance. Never refund its slack.
        del pending[0]
        self.consumed.append((phase, appearance, maximum, amount))
        return True

    async def bind(self, db, phase, *, certificate=None):
        self._checked()
        transaction, driver, changes = await _connection_state(db)
        if phase not in {"effect", "capture", "completion"}:
            raise HeaderBoundsError("memory_numeric_ledger_phase_invalid")
        prior = self.bindings.get(phase)
        if prior is not None and (prior[0] is not db or prior[1] is not transaction or prior[2] is not driver):
            raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
        if phase != "completion" and (db is not self.operation.db
                or transaction is not self.operation.transaction or driver is not self.operation.driver):
            raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
        if certificate is not None:
            from src.memory.composition_headers import validate_composition_certificate
            await validate_composition_certificate(db, certificate)
            if certificate.budget is not self.budget:
                raise HeaderBoundsError("memory_numeric_ledger_certificate_changed")
        self.bindings[phase] = (db, transaction, driver, changes, certificate)

    @contextmanager
    def phase(self, phase):
        self._checked()
        binding = self.bindings.get(phase)
        if binding is None or binding[0].sync_session.get_transaction() is not binding[1] or not binding[2].in_transaction:
            raise HeaderBoundsError("memory_numeric_ledger_writer_changed")
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
        if (phase not in {"effect", "capture", "completion"} or type(appearance) is not tuple
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


@dataclass(frozen=True, eq=False)
class HeaderCertificate:
    descriptor: ClosedRowDescriptor
    row_ids: tuple[str, ...]
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


async def validate_certificate(db, certificate):
    if (type(certificate) is not HeaderCertificate or certificate._seal is not _SEAL
            or certificate._issued_id != id(certificate) or certificate._db is not db):
        raise HeaderBoundsError("header_certificate_unavailable")
    transaction, driver, changes = await _connection_state(db)
    if (transaction is not certificate._transaction or driver is not certificate._driver
            or changes != certificate._changes):
        raise HeaderBoundsError("header_certificate_stale")


def _quoted(identifier):
    # Identifiers come only from the private singleton descriptor registry.
    return '"' + identifier + '"'


async def preflight_exact_rows(db, descriptor, exact_row_ids, remaining_bytes):
    if not any(descriptor is item for item in _DESCRIPTORS):
        raise HeaderBoundsError("header_descriptor_unavailable")
    if (type(exact_row_ids) is not tuple or len(exact_row_ids) > MAX_ROWS
            or any(type(value) is not str or not value for value in exact_row_ids)
            or type(remaining_bytes) is not int or not 0 <= remaining_bytes <= MAX_BYTES):
        raise HeaderBoundsError("header_request_bound")
    try:
        valid_ids = (len(set(exact_row_ids)) == len(exact_row_ids)
                     and all(len(value.encode("utf-8")) <= 512 for value in exact_row_ids))
    except UnicodeEncodeError:
        valid_ids = False
    if not valid_ids:
        raise HeaderBoundsError("header_request_bound")
    transaction, driver, changes = await _connection_state(db)
    from src.memory.composition_headers import validate_descriptor_schema
    schema_cost = await (await db.connection()).run_sync(lambda c: validate_descriptor_schema(c, descriptor))
    version = await db.scalar(text("SELECT sqlite_version()"))
    if tuple(map(int, version.split("."))) < (3, 43, 0):
        raise HeaderBoundsError("header_sqlite_version_unsupported")
    if await db.scalar(text("PRAGMA encoding")) != "UTF-8":
        raise HeaderBoundsError("header_encoding_unsupported")
    # The virtual pragma returns only bounded schema metadata, never defaults.
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
        rows = list(await db.execute(statement, {"key": identity}))
        if len(rows) != 1:
            raise HeaderBoundsError("header_row_unavailable")
        row = rows[0]
        if type(row[0]) is not int:
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
