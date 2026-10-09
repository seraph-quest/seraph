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

from sqlalchemy import Boolean, Float, Integer, text
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

MAX_ROWS = 128
MAX_BYTES = 1_048_576
_SEAL = object()


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


class HeaderReadBudget:
    """Shared byte evidence only; an original owner supplies all actual reads."""
    def __init__(self):
        self.remaining = MAX_BYTES
        self.references = set()

    async def certify(self, db, descriptor, row_ids):
        if not any(descriptor is item for item in _DESCRIPTORS):
            raise HeaderBoundsError("header_descriptor_unavailable")
        refs = {(descriptor.table, descriptor.key, identity) for identity in row_ids}
        if len(self.references | refs) > MAX_ROWS:
            raise HeaderBoundsError("header_reference_bound")
        certificate = await preflight_exact_rows(db, descriptor, tuple(row_ids), self.remaining)
        self.references |= refs
        self.remaining -= certificate.upper_bytes
        await validate_certificate(db, certificate)
        return certificate


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
    version = await db.scalar(text("SELECT sqlite_version()"))
    if tuple(map(int, version.split("."))) < (3, 43, 0):
        raise HeaderBoundsError("header_sqlite_version_unsupported")
    if await db.scalar(text("PRAGMA encoding")) != "UTF-8":
        raise HeaderBoundsError("header_encoding_unsupported")
    # The virtual pragma returns only bounded schema metadata, never defaults.
    schema = list(await db.execute(text(
        "SELECT CASE WHEN typeof(name)='text' THEN CASE WHEN octet_length(name)<=128 THEN name END END, "
        "CASE WHEN typeof(type)='text' THEN CASE WHEN octet_length(type)<=128 THEN type END END, "
        "\"notnull\",pk "
        "FROM pragma_table_info(:table) LIMIT :limit"),
        {"table": descriptor.table, "limit": len(descriptor.columns)+1}))
    names = [row[0] for row in schema]
    if (len(schema) != len(descriptor.columns) or set(names) != set(descriptor.columns)
            or any(name in {"rowid", "_rowid_", "oid"} for name in names)):
        raise HeaderBoundsError("header_schema_changed")
    expected = dict(zip(descriptor.columns, zip(descriptor.sql_types, descriptor.nullable)))
    for name, sql_type, notnull, pk in schema:
        if (type(sql_type) is not str or type(notnull) is not int or type(pk) is not int
                or sql_type.upper() != expected[name][0] or notnull != int(not expected[name][1])):
            raise HeaderBoundsError("header_schema_changed")
    query_fields = ["_rowid_"]
    for name in descriptor.columns:
        column = _quoted(name)
        query_fields.extend((f"typeof({column})", f"octet_length({column})",
                             f"CASE WHEN typeof({column}) IN ('integer','real') THEN {column} ELSE NULL END"))
    statement = text("SELECT " + ",".join(query_fields) + " FROM " + _quoted(descriptor.table)
                     + " WHERE " + _quoted(descriptor.key) + " COLLATE BINARY=:key LIMIT 2")
    rowids, costs = [], []
    total = 0
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
