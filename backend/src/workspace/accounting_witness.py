"""Dependency-free encoding shared by the runtime and managed lifecycle CLI."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import math
from pathlib import Path
from types import SimpleNamespace
from contextvars import ContextVar
from dataclasses import dataclass, field
from weakref import WeakSet, WeakValueDictionary

from src.workspace.production import ProductionWorkspace, ProductionWorkspaceReconciliationError, read_lifecycle_receipt, LIFECYCLE_PATH_ENV, CANONICAL_CONTAINER_WORKSPACE, BIND_IDENTITY_ENV


# Reviewed composition-continuity-projection.v1 persisted scalar allowlist.
# An unexpected/missing column blocks; no heartbeat exclusions.
COMPOSITION_FIELDS = {
    "messages": ("id", "session_id", "conversation_id", "thread_id", "owner_principal_id", "operator_session_id", "device_id", "channel", "transport", "correlation_id", "causation_id", "attachment_refs_json", "role", "content", "metadata_json", "step_number", "tool_used", "created_at"),
    "work_board_tasks": ("creation_sequence", "task_id", "owner_principal_id", "owner_session_id", "origin_session_id", "origin_thread_id", "goal_id", "goal_revision", "title", "body", "capability_id", "input_artifact_id", "pipeline_operation_id", "pipeline_slot", "typed_input_ref", "typed_input_digest", "executor_id", "assignee_id", "priority", "idempotency_scope", "idempotency_key", "idempotency_payload_digest", "idempotency_binding", "scheduled_at", "status", "block_kind", "block_reason", "block_source_status", "requires_review", "reviewer_id", "review_expires_at", "review_request_attempt_id", "review_request_fence", "review_request_revision", "review_request_digest", "review_request_evidence_json", "review_requested_at", "task_revision", "result_refs_json", "artifact_refs_json", "created_at", "updated_at", "completed_at", "archived_at"),
    "work_board_input_artifacts": ("artifact_id", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id", "capability_version", "idempotency_key", "payload_sha256", "typed_input_ref", "size_bytes", "state", "bound_task_id", "bound_task_revision", "created_at", "expires_at", "consumed_at", "revision", "metadata_digest", "document_metadata_json", "document_reserved_bytes"),
    "work_board_attempts": ("attempt_id", "task_id", "workflow_run_id", "task_revision_at_claim", "lease_owner", "lease_expires_at", "heartbeat_at", "fencing_token", "executor_id", "started_at", "ended_at", "cancel_requested_at", "outcome", "parent_handoff_context_json", "parent_handoff_digest", "receipt_refs_json", "created_at", "updated_at"),
    "work_board_review_intents": ("intent_id", "owner_principal_id", "owner_session_id", "task_id", "attempt_id", "workflow_run_id", "fencing_token", "task_revision", "request_digest", "evidence_refs_json", "status", "created_at"),
    "work_board_links": ("link_id", "owner_principal_id", "owner_session_id", "parent_task_id", "child_task_id", "current_handoff_id", "created_at"),
    "work_board_events": ("event_id", "task_id", "owner_principal_id", "owner_session_id", "actor_principal_id", "actor_session_id", "kind", "metadata_json", "mutation_idempotency_key", "mutation_request_digest", "created_at"),
    "work_board_evidence_dependencies": ("dependency_id", "task_id", "owner_principal_id", "owner_session_id", "goal_id", "source_kind", "canonical_source_id", "source_id", "source_digest", "span_digest", "resolved_token_json", "packet_revision", "packet_digest", "binding_task_revision", "executor_input_digest", "pipeline_operation_id", "pipeline_slot"),
    "work_board_handoffs": ("handoff_id", "schema_version", "owner_principal_id", "owner_session_id", "parent_task_id", "child_task_id", "link_id", "source_attempt_id", "workflow_run_id", "source_task_revision", "summary", "artifact_refs_json", "result_refs_json", "verification_json", "risks_json", "created_at"),
    "workflow_run_states": ("id", "run_identity", "root_run_identity", "parent_run_identity", "workflow_name", "tool_name", "session_id", "conversation_id", "operator_session_id", "status", "branch_kind", "branch_depth", "run_fingerprint", "arguments_json", "approval_context_json", "checkpoint_context_json", "artifact_paths_json", "continued_error_steps_json", "last_completed_step_id", "error", "heartbeat_at", "started_at", "updated_at", "finished_at", "metadata_json", "record_schema_version", "parent_job_id", "parent_fencing_token", "job_kind", "owner_kind", "owner_principal_id", "service_id", "goal_id", "goal_revision", "plan_revision", "candidate_id", "source_task_id", "composition_binding_json", "selected_context_reserved_bytes", "capability_version", "input_digest", "authority_digest", "budget_digest", "idempotency_scope", "idempotency_key", "idempotency_binding", "priority", "dependencies_json", "resource_claims_json", "declared_authority_json", "deadline_at", "lease_owner", "lease_expires_at", "fencing_token", "revision", "attempt_count", "max_attempts", "failure_reason", "checkpoint_receipts_json", "artifact_receipts_json", "effect_receipts_json", "github_read_revision_json", "github_read_observation_history_json", "github_capacity_closure_json", "result_digest", "result_summary"),
    "workflow_step_states": ("id", "run_identity", "workflow_name", "step_id", "step_index", "tool_name", "status", "arguments_json", "result_json", "result_summary", "artifact_paths_json", "error_kind", "error_summary", "checkpoint_json", "started_at", "updated_at", "completed_at"),
    "workflow_artifact_reviews": ("id", "run_identity", "root_run_identity", "parent_run_identity", "workflow_name", "artifact_path", "owner", "review_state", "reviewer", "decision", "approval_id", "metadata_json", "created_at", "updated_at", "decided_at"),
    "production_workflow_authority_states": ("id", "run_identity", "workflow_name", "scheduler_state_owner", "workflow_lease_id", "worker_owner", "lease_revision", "workflow_phase", "resumable_step_state", "replay_window", "recovery_authority", "safe_replay_decision", "blocked_replay_reason", "side_effect_status", "residual_risk", "transition_ledger_json", "metadata_json", "created_at", "updated_at"),
    "production_workflow_fault_receipts": ("id", "fault_key", "run_identity", "injection_method", "campaign_window", "recovery_result", "replay_decision", "duplicate_suppressed_count", "operator_intervention_required", "raw_receipt_handle", "residual_risk", "metadata_json", "created_at", "updated_at"),
    "production_workflow_side_effect_receipts": ("id", "reconciliation_id", "run_identity", "side_effect_kind", "idempotency_scope", "idempotency_key", "external_confirmation_state", "provider_receipt", "duplicate_suppression_receipt", "reconciliation_outcome", "manual_repair_state", "operator_replay_decision", "redacted_receipt_handle", "metadata_json", "created_at", "updated_at"),
    "runtime_composition_states": ("runtime_domain", "owner_kind", "epoch", "composition_digest", "state", "recovery_receipt_ref"),
}

COMPOSITION_SCHEMA = "composition-continuity-projection.v1"
NATIVE_EXTENSION_V2_FIELDS = {
    "sessions": ("id", "owner_principal_id", "title", "created_at", "updated_at"),
    "audit_events": ("id", "session_id", "actor", "event_type", "tool_name", "risk_level", "policy_mode", "summary", "details_json", "created_at"),
    "memory_episodes": ("id", "session_id", "episode_type", "summary", "content", "source_message_id", "source_tool_name", "source_role", "subject_entity_id", "project_entity_id", "salience", "confidence", "metadata_json", "observed_at", "created_at"),
}
NATIVE_EXTENSION_V2_VERSION = "native-composition-turn.v2"
NATIVE_EXTENSION_V2_DIGEST = hashlib.sha256(json.dumps({"version": NATIVE_EXTENSION_V2_VERSION,
    "fields": NATIVE_EXTENSION_V2_FIELDS, "joins": ["conversation_context", "selected-message-episode", "closed-composition-audit-predecessor",
        "legacy_job_session_fk", "audit_event_session_fk", "closed-restore-audit-predecessor"],
    "codec": {"memory_episodes.salience": "finite-float64.hex", "memory_episodes.confidence": "finite-float64.hex"}},
    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
NATIVE_EXTENSION_FIELDS = {**NATIVE_EXTENSION_V2_FIELDS,
    "approval_requests": ("id", "session_id", "conversation_id", "thread_id", "owner_principal_id", "operator_session_id", "device_id", "channel", "transport", "correlation_id", "causation_id", "attachment_refs_json", "challenge", "action", "expires_at", "tool_name", "risk_level", "status", "fingerprint", "summary", "details_json", "created_at", "resolved_at"),
}
NATIVE_EXTENSION_VERSION = "native-composition-turn.v3"
NATIVE_EXTENSION_DIGEST = hashlib.sha256(json.dumps({"version": NATIVE_EXTENSION_VERSION,
    "parent_version": NATIVE_EXTENSION_V2_VERSION, "parent_digest": NATIVE_EXTENSION_V2_DIGEST,
    "fields": NATIVE_EXTENSION_FIELDS,
    "joins": ["selected-controlled-clarification", "selected-controlled-approval", "approval_request_session_fk"]},
    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
RETAINED_FIELDS = {**COMPOSITION_FIELDS, **NATIVE_EXTENSION_FIELDS}
COMPOSITION_KEYS = {name: fields[0] for name, fields in RETAINED_FIELDS.items()}
COMPOSITION_KEYS["work_board_tasks"] = "task_id"
COMPOSITION_KEYS["workflow_run_states"] = "run_identity"
_HELD_COMPOSITION_WORKSPACE = ContextVar("held_composition_workspace", default=None)
_MEMORY_PUBLICATION_SEAL = object()
_MEMORY_PUBLICATIONS = WeakValueDictionary()
_ARMED_MEMORY_PUBLICATION = ContextVar("armed_native_memory_publication", default=None)

MEMORY_REFERENCE_PROFILE = "native-composition-memory-reference.v2"
MEMORY_ORIGINAL_CHECKPOINT = "memory:original-reference.v2"
MEMORY_CURRENT_CHECKPOINT = "memory:current-reference.v2"
_MEMORY_ORIGINAL_FIELDS = frozenset(("schema_version", "profile", "invocation_ref", "claim_ref",
    "candidate_digest", "composition_binding_digest", "method", "owner_principal_id",
    "operator_session_id", "original_deadline", "source_binding_digest", "original_effect_digest",
    "original_audit", "refs", "rows_digest", "encoded_bytes"))
_MEMORY_CURRENT_FIELDS = frozenset(("schema_version", "profile", "original_checkpoint_digest",
    "projection_revision", "state", "reason_code", "refs", "absences", "rows", "rows_digest",
    "encoded_bytes", "owner_operation_kind", "owner_events", "selected_delta_digest"))
_MEMORY_UNKNOWN_REASONS = frozenset(("canonical_body_over_limit", "canonical_bound_not_certified",
    "canonical_body_unavailable", "current_source_unavailable", "current_mac_unavailable",
    "selected_goal_deleted", "original_projection_unavailable", "original_projection_invalid"))
_MEMORY_NATIVE_OPERATIONS = frozenset(("memory.propose", "memory.applyReviewed", "memory.forget"))


def preflight_native_memory_reference_payload(payload, *, current=False):
    """Closed byte-shape parser only; no source, row, MAC or effect authority.

    Actual owner publication and current-row validation remain mandatory and
    unavailable until the complete source-issued contract is installed.
    """
    import re
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, MAX_ROWS, MEMORY_DESCRIPTORS
    def denied():
        raise HeaderBoundsError("memory_reference_payload_invalid")
    def sha(value):
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            denied()
    def key(value):
        try:
            valid = type(value) is str and bool(value) and len(value.encode("utf-8")) <= 512
        except UnicodeEncodeError:
            valid = False
        if not valid:
            denied()
    def bounded_integer(value, upper):
        if type(value) is not int or not 0 <= value <= upper:
            denied()
    def reference(value):
        if (type(value) is not dict or set(value) != {"table", "key"}
                or type(value["table"]) is not str
                or value["table"] not in {*RETAINED_FIELDS, *MEMORY_DESCRIPTORS}):
            denied()
        key(value["key"])
        return value["table"], value["key"]
    def reference_list(values):
        if type(values) is not list or len(values) > MAX_ROWS:
            denied()
        result = [reference(item) for item in values]
        if result != sorted(set(result)):
            denied()
        return result
    fields = _MEMORY_CURRENT_FIELDS if current else _MEMORY_ORIGINAL_FIELDS
    if (type(current) is not bool or type(payload) is not dict or set(payload) != fields
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 2
            or payload["profile"] != MEMORY_REFERENCE_PROFILE):
        denied()
    refs = reference_list(payload["refs"])
    bounded_integer(payload["encoded_bytes"], MAX_BYTES)
    if current:
        bounded_integer(payload["projection_revision"], 2**63 - 1)
        if (payload["projection_revision"] < 1 or type(payload["owner_operation_kind"]) is not str
                or payload["owner_operation_kind"] not in _MEMORY_NATIVE_OPERATIONS):
            denied()
        sha(payload["selected_delta_digest"])
        events = reference_list(payload["owner_events"])
        if len(events) > 2 or any(table not in {"audit_events", "work_board_events"} for table, _ in events):
            denied()
        if type(payload["absences"]) is not list or len(payload["absences"]) > MAX_ROWS:
            denied()
        absent = []
        for item in payload["absences"]:
            if type(item) is not dict or set(item) != {"ref", "reason", "original_binding_digest"}:
                denied()
            ref = reference(item["ref"])
            if (type(item["reason"]) is not str
                    or (ref[0], item["reason"]) not in {("goals", "goal_deleted"), ("memories", "memory_deleted")}
                    or ref not in refs):
                denied()
            sha(item["original_binding_digest"])
            absent.append(ref)
        if absent != sorted(set(absent)) or len(set((*refs, *events))) > MAX_ROWS:
            denied()
        if payload["original_checkpoint_digest"] is None:
            if payload["state"] != "unknown" or type(payload["reason_code"]) is not str or payload["reason_code"] not in {
                    "original_projection_unavailable", "original_projection_invalid"}:
                denied()
        else:
            sha(payload["original_checkpoint_digest"])
        if payload["state"] == "unknown":
            if (type(payload["reason_code"]) is not str or payload["reason_code"] not in _MEMORY_UNKNOWN_REASONS
                    or payload["rows"] != [] or type(payload["rows"]) is not list
                    or payload["rows_digest"] is not None):
                denied()
        elif payload["state"] == "validated":
            if (payload["reason_code"] is not None or absent or type(payload["rows"]) is not list
                    or len(payload["rows"]) > MAX_ROWS):
                denied()
            sha(payload["rows_digest"])
            row_refs = []
            for row in payload["rows"]:
                if type(row) is not dict or set(row) != {"ref", "tuple_digest", "encoded_bytes"}:
                    denied()
                row_refs.append(reference(row["ref"]))
                sha(row["tuple_digest"])
                bounded_integer(row["encoded_bytes"], MAX_BYTES)
            if (row_refs != sorted(set((*refs, *events)))
                    or sum(row["encoded_bytes"] for row in payload["rows"]) != payload["encoded_bytes"]):
                denied()
        else:
            denied()
    else:
        for name in ("invocation_ref", "claim_ref", "owner_principal_id", "operator_session_id"):
            key(payload[name])
        if ("workflow_run_states", payload["invocation_ref"]) not in refs:
            denied()
        for name in ("candidate_digest", "composition_binding_digest", "source_binding_digest",
                     "original_effect_digest", "rows_digest"):
            sha(payload[name])
        if type(payload["method"]) is not str or payload["method"] not in _MEMORY_NATIVE_OPERATIONS:
            denied()
        audit = payload["original_audit"]
        if type(audit) is not dict or set(audit) != {"ref", "tuple_digest"}:
            denied()
        audit_ref = reference(audit["ref"])
        if audit_ref[0] != "audit_events" or audit_ref not in refs:
            denied()
        sha(audit["tuple_digest"])
        try:
            deadline = datetime.fromisoformat(payload["original_deadline"])
            if deadline.tzinfo is None or deadline.astimezone(timezone.utc).isoformat() != payload["original_deadline"]:
                denied()
        except (TypeError, ValueError):
            denied()
    try:
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError):
        denied()
    if len(encoded) > 262_144:
        raise HeaderBoundsError("memory_reference_envelope_bound")
    return len(encoded)


def _native_memory_unknown_replacement_upper_bytes(refs, *, original_checkpoint_digest,
                                                   owner_operation_kind, owner_events):
    """Structural upper bound for one Current checkpoint, not owner permission.

    Charge this for each affected job before effects, plus immutable Original,
    other current body/envelope appearances and the containing run/recovery
    encoding. Actual identities are mandatory; no guessed future reference.
    """
    reasons = (_MEMORY_UNKNOWN_REASONS if original_checkpoint_digest is not None else
               {"original_projection_unavailable", "original_projection_invalid"})
    payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": original_checkpoint_digest,
        "projection_revision": 2**63 - 1, "state": "unknown",
        "reason_code": max(reasons, key=lambda value: len(value.encode("utf-8"))),
        "refs": refs, "absences": [], "rows": [], "rows_digest": None,
        "encoded_bytes": 0, "owner_operation_kind": owner_operation_kind,
        "owner_events": owner_events, "selected_delta_digest": "f" * 64}
    # First certify all caller shapes and finite references, before traversing
    # optional absence candidates. No original source/binding grant follows.
    preflight_native_memory_reference_payload(payload, current=True)
    payload["absences"] = [{"ref": ref,
        "reason": "goal_deleted" if ref["table"] == "goals" else "memory_deleted",
        "original_binding_digest": "f" * 64} for ref in refs if ref["table"] in {"goals", "memories"}]
    # Unknown's own metadata byte count is self-describing. Integer decimal
    # width reaches a fixed point monotonically within this envelope ceiling.
    for _ in range(8):
        measured = preflight_native_memory_reference_payload(payload, current=True)
        if payload["encoded_bytes"] == measured:
            break
        payload["encoded_bytes"] = measured
    else:
        raise ProductionWorkspaceReconciliationError("memory_unknown_reserve_unavailable")
    checkpoint = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "state_digest": "f" * 64,
                  "safe": True, "payload": payload}
    return len(json.dumps(checkpoint, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8"))


def preflight_native_memory_reference_journal(raw):
    """Bounded closed checkpoint parser, never a mint/copy/adoption permission.

    Invalid Original is reported to the actual reduction owner; it cannot be
    replaced with an invented Original. This helper only parses valid shapes.
    The original owner must first certify this raw column's whole-row header.
    """
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    from src.workflows.job_runtime import _digest
    records = strict_json_loads(raw)
    if type(records) is not list:
        raise HeaderBoundsError("memory_reference_journal_invalid")
    found = {}
    for record in records:
        if type(record) is not dict:
            raise HeaderBoundsError("memory_reference_journal_invalid")
        identifier = record.get("checkpoint_id")
        if identifier not in (MEMORY_ORIGINAL_CHECKPOINT, MEMORY_CURRENT_CHECKPOINT):
            continue
        if (identifier in found or set(record) != {"checkpoint_id", "state_digest", "safe", "payload"}
                or record["safe"] is not True):
            raise HeaderBoundsError("memory_reference_journal_invalid")
        preflight_native_memory_reference_payload(record["payload"], current=identifier == MEMORY_CURRENT_CHECKPOINT)
        if record["state_digest"] != _digest(record["payload"]):
            raise HeaderBoundsError("memory_reference_journal_invalid")
        found[identifier] = record
    return found.get(MEMORY_ORIGINAL_CHECKPOINT), found.get(MEMORY_CURRENT_CHECKPOINT)


def _splice_native_memory_current_checkpoint(raw, replacement):
    """Bounded lexical replacement only; never publication or owner authority.

    Preserve every character outside the old Current record, including other
    checkpoint escapes and whitespace. The actual owner must separately bind
    the original journal, transaction and protected revision in its SQL CAS.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, strict_json_loads
    from src.workflows.job_runtime import _canonical, _digest
    # Old Original/Current can be invalid precisely when the real reduction
    # owner must publish Unknown. Parsing their lexical containers here does
    # not attest their payloads; preserve Original without adopting it.
    records = strict_json_loads(raw)
    if type(records) is not list or any(type(record) is not dict for record in records):
        raise HeaderBoundsError("memory_reference_journal_invalid")
    for identifier in (MEMORY_ORIGINAL_CHECKPOINT, MEMORY_CURRENT_CHECKPOINT):
        if sum(record.get("checkpoint_id") == identifier for record in records) > 1:
            raise HeaderBoundsError("memory_reference_journal_invalid")
    if (type(replacement) is not dict
            or set(replacement) != {"checkpoint_id", "state_digest", "safe", "payload"}
            or replacement.get("checkpoint_id") != MEMORY_CURRENT_CHECKPOINT
            or replacement["safe"] is not True
            or type(replacement["state_digest"]) is not str):
        raise HeaderBoundsError("memory_reference_journal_invalid")
    preflight_native_memory_reference_payload(replacement["payload"], current=True)
    if replacement["state_digest"] != _digest(replacement["payload"]):
        raise HeaderBoundsError("memory_reference_journal_invalid")
    # The exact wrapper parser checks shape, safe=True and the existing runtime
    # digest codec. This does not authorize the Current represented by it.
    try:
        encoded = _canonical(replacement)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise HeaderBoundsError("memory_reference_journal_invalid") from None
    preflight_native_memory_reference_journal("[" + encoded + "]")
    decoder = json.JSONDecoder()
    position = 0
    whitespace = " \t\r\n"
    while raw[position] in whitespace:
        position += 1
    position += 1  # Strict parsing already established the opening array.
    spans = []
    for _record in records:
        while raw[position] in whitespace:
            position += 1
        start = position
        _, position = decoder.raw_decode(raw, position)
        spans.append((start, position))
        while raw[position] in whitespace:
            position += 1
        if raw[position] == ",":
            position += 1
    matches = [index for index, record in enumerate(records)
               if record.get("checkpoint_id") == MEMORY_CURRENT_CHECKPOINT]
    if matches:
        start, end = spans[matches[0]]
        result = raw[:start] + encoded + raw[end:]
    else:
        # Insert after the last record, preserving its trailing whitespace, or
        # directly after '[' for an empty array. Only one delimiter is added.
        insert_at = spans[-1][1] if spans else position
        result = raw[:insert_at] + ("," if spans else "") + encoded + raw[insert_at:]
    if len(result.encode("utf-8")) > MAX_BYTES:
        raise HeaderBoundsError("header_json_bound")
    return result


def _native_memory_planned_sql_row(instance, changes=None, *, transient=False):
    """Closed deterministic SQLite binding of a real transient constructor row.

    This byte projection does not enroll the instance or issue source authority.
    Its actual owner still binds exact instance identity, complete reservation
    and postflush raw tuple equality before capture/publication.
    """
    from sqlalchemy import Boolean, DateTime, Enum, Float, Integer, String, inspect
    from sqlalchemy.dialects.sqlite import dialect
    from sqlmodel.sql.sqltypes import AutoString
    from src.db.models import AuditEvent, WorkflowRunState
    from src.memory.header_bounds import HeaderBoundsError, MEMORY_DESCRIPTORS, _MEMORY_MODELS, AUDIT_EVENT, WRS_BY_RUN
    table = getattr(type(instance), "__tablename__", None)
    model = {"audit_events": AuditEvent, "workflow_run_states": WorkflowRunState}.get(table, _MEMORY_MODELS.get(table))
    if model is None or type(instance) is not model or (transient and not inspect(instance).transient):
        raise HeaderBoundsError("memory_transient_row_unavailable")
    descriptor = {"audit_events": AUDIT_EVENT, "workflow_run_states": WRS_BY_RUN}.get(table)
    if descriptor is None:
        descriptor = MEMORY_DESCRIPTORS[table]
    from sqlalchemy.orm.attributes import NO_VALUE
    state=inspect(instance)
    if changes is None:
        changes={}
    if type(changes) is not dict or not set(changes)<=set(descriptor.columns):
        raise HeaderBoundsError("memory_transient_bind_value_invalid")
    selected_dialect = dialect()
    row = {}
    allowed_types = {String, Integer, Boolean, Float, DateTime, Enum, AutoString}
    for name, kind, nullable in zip(descriptor.columns, descriptor.kinds, descriptor.nullable):
        column = model.__table__.columns[name]
        if type(column.type) not in allowed_types:
            raise HeaderBoundsError("memory_transient_bind_type_unsupported")
        if name in changes:
            value=changes[name]
        else:
            value=state.attrs[name].loaded_value
            if value is NO_VALUE:
                raise HeaderBoundsError(
                    "memory_transient_bind_value_invalid" if transient else "memory_planned_row_not_loaded"
                )
        try:
            processor = column.type.dialect_impl(selected_dialect).bind_processor(selected_dialect)
            value = processor(value) if processor is not None else value
        except (TypeError, ValueError, OverflowError):
            raise HeaderBoundsError("memory_transient_bind_value_invalid") from None
        if value is None:
            if not nullable:
                raise HeaderBoundsError("memory_transient_bind_value_invalid")
        elif ((kind == "text" and type(value) is not str)
              or (kind == "integer" and (type(value) is not int or not -(2**63) <= value < 2**63))
              or (kind == "real" and (type(value) is not float or not math.isfinite(value)))):
            raise HeaderBoundsError("memory_transient_bind_value_invalid")
        row[name] = value
    return row


def _native_memory_transient_sql_row(instance):
    """Bind a real original transient row; no Source/constructor enrollment."""
    return _native_memory_planned_sql_row(instance, transient=True)


def _native_memory_row_bytes(descriptor, key, row):
    """The closed raw Memory-reference tuple; never a Source permission."""
    from src.memory.header_bounds import HeaderBoundsError
    if (set(row) != set(descriptor.columns) or type(key) is not str
            or not key or len(key.encode("utf-8")) > 512):
        raise HeaderBoundsError("memory_reference_row_invalid")
    return json.dumps(["native-composition-memory.v1", descriptor.table, key,
        [[name, row[name].hex() if type(row[name]) is float else row[name]]
         for name in descriptor.columns]], ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


async def _native_memory_read_reference_rows(db, references, header_budget):
    """Read exact source-selected raw tuples after their whole-row certificate.

    This byte helper grants no Source, publication, adoption or cleanup right.
    The owner must supply its genuine selected graph and validate its semantics.
    """
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS, HeaderReadBudget, HeaderBoundsError
    from sqlalchemy import text
    if type(references) is not tuple or type(header_budget) is not HeaderReadBudget:
        raise HeaderBoundsError("memory_reference_row_invalid")
    if (len(references) > 128 or any(type(ref) is not tuple or len(ref) != 2
            or type(ref[0]) is not str or type(ref[1]) is not str for ref in references)
            or references != tuple(sorted(set(references)))):
        raise HeaderBoundsError("memory_reference_row_invalid")
    result = []
    for table, key in references:
        descriptor = COMPOSITION_DESCRIPTORS.get(table)
        if descriptor is None or type(key) is not str or not key or len(key.encode("utf-8")) > 512:
            raise HeaderBoundsError("memory_reference_row_invalid")
        await header_budget.certify(db, descriptor, (key,))
        columns = ",".join('"' + name + '"' for name in descriptor.columns)
        parameter = int(_composition_address(table, key)) if table == "work_board_events" else key
        rows = (await db.execute(text(
            f'SELECT {columns} FROM "{table}" WHERE "{descriptor.key}"=:key'),
            {"key": parameter})).all()
        if len(rows) != 1:
            raise HeaderBoundsError("memory_reference_row_missing")
        row = dict(zip(descriptor.columns, rows[0]))
        encoded = _native_memory_row_bytes(descriptor, key, row)
        result.append((descriptor, key, row, encoded))
    return tuple(result)


def _native_memory_own_journal_projection(raw, run_identity):
    """Replace exactly the adopted three lexical values in a temporary copy."""
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    records = strict_json_loads(raw)
    _, current = preflight_native_memory_reference_journal(raw)
    if current is None or current["payload"]["state"] != "validated":
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    record_index = next(index for index, record in enumerate(records)
        if record.get("checkpoint_id") == MEMORY_CURRENT_CHECKPOINT)
    own = [index for index, row in enumerate(current["payload"]["rows"])
        if row["ref"] == {"table": "workflow_run_states", "key": run_identity}]
    if len(own) != 1:
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    replacements = {
        (record_index, "state_digest"): '"' + "0" * 64 + '"',
        (record_index, "payload", "rows", own[0], "tuple_digest"): '"' + "0" * 64 + '"',
        (record_index, "payload", "rows", own[0], "encoded_bytes"): "0",
    }
    decoder = json.JSONDecoder()
    spans = []
    def whitespace(position):
        while position < len(raw) and raw[position] in " \t\r\n":
            position += 1
        return position
    def visit(position, path):
        position = whitespace(position)
        start = position
        if path in replacements:
            _, end = decoder.raw_decode(raw, position)
            spans.append((start, end, replacements[path]))
            return end
        if raw[position] == "{":
            position = whitespace(position + 1)
            while raw[position] != "}":
                key, position = decoder.raw_decode(raw, position)
                position = whitespace(position)
                if raw[position] != ":":
                    raise HeaderBoundsError("memory_reference_journal_invalid")
                position = whitespace(visit(position + 1, (*path, key)))
                if raw[position] == ",":
                    position = whitespace(position + 1)
            return position + 1
        if raw[position] == "[":
            position, index = whitespace(position + 1), 0
            while raw[position] != "]":
                position = whitespace(visit(position, (*path, index)))
                index += 1
                if raw[position] == ",":
                    position = whitespace(position + 1)
            return position + 1
        return decoder.raw_decode(raw, position)[1]
    visit(0, ())
    if len(spans) != 3:
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    for start, end, replacement in sorted(spans, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    return raw


def _native_memory_current_rows_digest(rows, run_identity):
    """The adopted aggregate uses the same two own-row placeholders."""
    from src.workflows.job_runtime import _digest
    from src.memory.header_bounds import HeaderBoundsError
    normalized = []
    count = 0
    for row in rows:
        value = dict(row)
        if row["ref"] == {"table": "workflow_run_states", "key": run_identity}:
            value.update(tuple_digest="0" * 64, encoded_bytes=0)
            count += 1
        normalized.append(value)
    if count != 1:
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    return _digest(normalized)


def _native_memory_prepare_current_row(raw_row, payload, *, header_budget):
    """Finite numeric convergence over real pending WRS values, no writer grant.

    The caller must first establish the genuine owner/readset. This helper
    never mutates its input, writes SQL, or installs a protected checkpoint.
    Every temporary tuple appearance consumes the original numeric frame.
    """
    from copy import deepcopy
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, WRS_BY_RUN, MAX_BYTES
    from src.workflows.job_runtime import _canonical, _digest
    if type(header_budget) is not HeaderReadBudget:
        raise HeaderBoundsError("memory_current_budget_unavailable")
    row, payload = dict(raw_row), deepcopy(payload)
    identity = row["run_identity"]
    own = [item for item in payload["rows"]
           if item["ref"] == {"table": "workflow_run_states", "key": identity}]
    if len(own) != 1:
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    own = own[0]
    own.update(tuple_digest="0" * 64, encoded_bytes=0)
    payload["rows_digest"] = _native_memory_current_rows_digest(payload["rows"], identity)
    original_journal = row["checkpoint_receipts_json"]
    def build():
        payload["encoded_bytes"] = sum(item["encoded_bytes"] for item in payload["rows"])
        preflight_native_memory_reference_payload(payload, current=True)
        record = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "safe": True,
                  "state_digest": _digest(payload), "payload": payload}
        journal = _splice_native_memory_current_checkpoint(original_journal, record)
        # Count all temporary raw appearances before encoding the full tuple.
        # Six times UTF8 is a conservative bound for canonical ASCII escaping.
        candidate = dict(row, checkpoint_receipts_json=journal)
        upper = len(_canonical(["native-composition-memory.v1", WRS_BY_RUN.table, identity,
            [[name, None] for name in WRS_BY_RUN.columns]]).encode("utf-8"))
        upper += sum(6 * len(value.encode("utf-8")) + 2 if type(value) is str
                     else 32 if value is not None else 0 for value in candidate.values())
        header_budget.debit(upper)
        encoded = _native_memory_row_bytes(WRS_BY_RUN, identity, candidate)
        if len(encoded) > MAX_BYTES:
            raise HeaderBoundsError("canonical_bound_not_certified")
        return candidate, record, encoded
    for _ in range(32):
        candidate, record, encoded = build()
        measured = len(encoded)
        if measured == own["encoded_bytes"]:
            break
        if measured < own["encoded_bytes"]:
            raise HeaderBoundsError("memory_current_count_not_convergent")
        own["encoded_bytes"] = measured
    else:
        raise HeaderBoundsError("memory_current_count_not_convergent")
    projected = dict(candidate)
    # The normalized temporary tuple is also a charged byte appearance.
    header_budget.debit(len(encoded))
    projected["checkpoint_receipts_json"] = _native_memory_own_journal_projection(
        candidate["checkpoint_receipts_json"], identity)
    own["tuple_digest"] = hashlib.sha256(_native_memory_row_bytes(WRS_BY_RUN, identity, projected)).hexdigest()
    candidate, record, final_bytes = build()
    if len(final_bytes) != measured or record["payload"]["encoded_bytes"] != sum(
            item["encoded_bytes"] for item in record["payload"]["rows"]):
        raise HeaderBoundsError("memory_current_count_not_convergent")
    return candidate, record


def _native_memory_selected_delta_preimage(new_rows, updates, owner_events):
    """Closed source-produced SQL binds, not a reconstructed owner effect."""
    from src.memory.header_bounds import HeaderBoundsError, COMPOSITION_DESCRIPTORS
    from src.workflows.job_runtime import _canonical
    def selected(values, field):
        result = []
        for descriptor, key, row in values:
            if (not any(descriptor is item for item in COMPOSITION_DESCRIPTORS.values())
                    or type(row) is not dict or type(key) is not str or not key
                    or len(key.encode("utf-8")) > 512):
                raise HeaderBoundsError("memory_reference_row_invalid")
            if any(type(value) is float and not math.isfinite(value) for value in row.values()):
                raise HeaderBoundsError("memory_reference_row_invalid")
            if field == "values":
                _native_memory_row_bytes(descriptor, key, row)
            elif not set(row) <= set(descriptor.columns):
                raise HeaderBoundsError("memory_reference_row_invalid")
            # Causal preimages retain exact resolved SQLite bind values. The
            # separate raw tuple codec's float.hex encoding is unchanged.
            encoded = dict(row)
            if any(value is not None and type(value) not in {str, int, float} for value in encoded.values()):
                raise HeaderBoundsError("memory_reference_row_invalid")
            result.append({"ref": {"table": descriptor.table, "key": key}, field: encoded})
        result.sort(key=lambda value: (value["ref"]["table"], value["ref"]["key"]))
        if len({(item["ref"]["table"], item["ref"]["key"]) for item in result}) != len(result):
            raise HeaderBoundsError("memory_reference_row_invalid")
        return result
    if any(table not in {"audit_events", "work_board_events"} or type(key) is not str
           or not key or len(key.encode("utf-8")) > 512 for table, key in owner_events):
        raise HeaderBoundsError("memory_reference_row_invalid")
    value = {"schema_version": 1, "new_rows": selected(new_rows, "values"),
             "updates": selected(updates, "columns"),
             "owner_events": [{"table": table, "key": key} for table, key in sorted(set(owner_events))]}
    # All leaves are now closed raw SQLite scalars. The existing serializer's
    # default=str is never relied upon for an unsupported object/nonfinite.
    _canonical(value)
    return value


async def reserve_native_memory_admission_run(db,run,admission):
    """Resource-only reservation immediately before the original WRS write.

    The actual admission owner remains responsible for authority/current rows.
    Future M5 constructors/results are separately reserved before their effect.
    No opaque receipt, callback, JSON or byte certificate grants admission here.
    """
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, WRS_BY_RUN, _connection_state
    from src.runtime_plugins.memory_producer import NativeMemoryMutationAdmission, candidate_context
    from sqlalchemy import inspect
    from src.db.models import WorkflowRunState
    if (type(admission) is not NativeMemoryMutationAdmission or type(run) is not WorkflowRunState
            or not inspect(run).transient or type(admission.header_budget) is not HeaderReadBudget):
        raise HeaderBoundsError("memory_admission_reserve_unavailable")
    await _connection_state(db)
    expected=candidate_context(admission)
    if run.checkpoint_context_json!=expected or run.input_digest!=admission.candidate_digest:
        raise HeaderBoundsError("memory_admission_reserve_changed")
    row=_native_memory_transient_sql_row(run)
    # Exact currently produced row/container, including candidate and its known
    # result=None wrapper, with the original complete SQLite scalar encoding.
    encoded=json.dumps(["native-composition-memory.v1",WRS_BY_RUN.table,run.run_identity,
        [[name,row[name].hex() if type(row[name]) is float else row[name]] for name in WRS_BY_RUN.columns]],
        ensure_ascii=True,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
    admission.header_budget.reserve_future_row(WRS_BY_RUN,run.run_identity,len(encoded))
    return len(encoded)


async def reserve_native_memory_pending_run(db, run, pending_changes, header_budget, *, outputs=()):
    """Numeric prewrite reserve for an actual original owner's staged WRS.

    This deliberately grants no claim, transition or checkpoint permission.
    Each actual public/private output appearance must be supplied by its
    original owner, in addition to the complete pending raw tuple.
    """
    from src.db.models import WorkflowRunState
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, WRS_BY_RUN, _connection_state
    from src.workflows.job_runtime import _canonical
    if (type(run) is not WorkflowRunState or type(header_budget) is not HeaderReadBudget
            or type(outputs) is not tuple):
        raise HeaderBoundsError("memory_pending_reserve_unavailable")
    await _connection_state(db)
    row = _native_memory_planned_sql_row(run, pending_changes)
    amount = len(_native_memory_row_bytes(WRS_BY_RUN, run.run_identity, row))
    for output in outputs:
        amount += len(_canonical(output).encode("utf-8"))
    header_budget.debit(amount)
    return amount


def _native_memory_statement_signature(statement):
    """Bind the original WHERE and the actual SQLite processed parameters."""
    from sqlalchemy.dialects.sqlite import dialect
    from src.workflows.job_runtime import _canonical
    compiled = statement.compile(dialect=dialect())
    parameters = compiled.construct_params()
    processed = {}
    for name, value in parameters.items():
        processor = compiled._bind_processors.get(name)
        value = processor(value) if processor is not None else value
        if value is not None and type(value) not in {str, int, float}:
            raise ProductionWorkspaceReconciliationError("memory_publication_bind_unsupported")
        if type(value) is float and not math.isfinite(value):
            raise ProductionWorkspaceReconciliationError("memory_publication_bind_unsupported")
        processed[name] = value
    return str(compiled), _canonical(processed)


@dataclass(eq=False)
class _NativeMemoryPublication:
    db: object
    run: object
    original_owner: object
    statement: object
    original_signature: tuple
    statement_signature: tuple
    before: dict
    after: dict
    certificate: object
    seal: object = field(default=_MEMORY_PUBLICATION_SEAL, repr=False)
    consumed: bool = False
    applied: bool = False


def _consume_native_memory_publication(db, statement, run_id, before, after, previous, current):
    """The journal hook can consume only the armed, exact original execute."""
    from src.memory.composition_headers import current_budget, _validate, _certify_current_memory_snapshot_on_connection
    from src.workflows.job_runtime import _MEMORY_NEGATIVE_OWNERS, _original_memory_maintenance_scope
    plan = _ARMED_MEMORY_PUBLICATION.get()
    if (type(plan) is not _NativeMemoryPublication or plan.seal is not _MEMORY_PUBLICATION_SEAL
            or _MEMORY_PUBLICATIONS.get(id(plan)) is not plan or plan.consumed or plan.applied
            or plan.db is not db or statement is not plan.statement
            or run_id != plan.before["run_identity"]
            or before != plan.before["checkpoint_receipts_json"]
            or after != plan.after["checkpoint_receipts_json"]
            or db.sync_session.get_transaction() is not plan.original_owner.tx
            or _MEMORY_NEGATIVE_OWNERS.get(id(plan.original_owner)) is not plan.original_owner
            or _original_memory_maintenance_scope(plan.original_owner._repository) is not plan.original_owner._scope
            or current_budget() is not plan.original_owner.header_budget
            or _native_memory_statement_signature(statement) != plan.statement_signature
            or _native_memory_statement_signature(plan.original_owner.statement) != plan.original_signature):
        raise ProductionWorkspaceReconciliationError("composition_memory_retention_unavailable")
    connection = db.sync_session.connection()
    _validate(connection, plan.certificate)
    current_certificate = _certify_current_memory_snapshot_on_connection(connection, plan.original_owner.header_budget)
    if current_certificate is not plan.certificate:
        raise ProductionWorkspaceReconciliationError("memory_publication_snapshot_changed")
    if (connection.connection.driver_connection is not plan.original_owner.driver
            or _sql(connection, "SELECT total_changes()").scalar_one() != plan.original_owner.total_changes):
        raise ProductionWorkspaceReconciliationError("memory_publication_writer_changed")
    _, raw, _ = _native_memory_reference_row_on_connection(connection, "workflow_run_states", run_id)
    if raw != plan.before or previous.get(MEMORY_ORIGINAL_CHECKPOINT) != current.get(MEMORY_ORIGINAL_CHECKPOINT):
        raise ProductionWorkspaceReconciliationError("memory_publication_original_changed")
    # A cold read never grants this permission: registration came exclusively
    # from the actual lexical repository owner, revalidated immediately before
    # arming. Unknown is the only allowed publication in this owner family.
    replacement = current.get(MEMORY_CURRENT_CHECKPOINT)
    if replacement is None or replacement["payload"]["state"] != "unknown":
        raise ProductionWorkspaceReconciliationError("memory_publication_unknown_required")
    plan.consumed = True
    return (replacement,)


async def prepare_native_memory_unknown(db, run, *, original_owner):
    """Stage an original negative owner's Current only; never mint Original."""
    from src.workflows.job_runtime import _validate_original_memory_negative_owner, _canonical, _digest
    from src.memory.header_bounds import HeaderReadBudget, WRS_BY_RUN
    from src.memory.universe import native_memory_universe
    from src.memory.composition_headers import _certify_current_memory_snapshot, snapshot_reads
    owner = await _validate_original_memory_negative_owner(db, run, original_owner)
    if type(owner.header_budget) is not HeaderReadBudget:
        raise ProductionWorkspaceReconciliationError("memory_negative_budget_unavailable")
    await native_memory_universe(db)
    certificate = await _certify_current_memory_snapshot(db, owner.header_budget)
    def inspect_original(connection):
        with snapshot_reads(certificate):
            _, before, _ = _native_memory_reference_row_on_connection(connection, "workflow_run_states", run.run_identity)
            _checked_memory_reference_row(connection, before)
            return before
    before = await (await db.connection()).run_sync(inspect_original)
    if before != dict(owner.before_values):
        raise ProductionWorkspaceReconciliationError("memory_negative_original_changed")
    original, current = preflight_native_memory_reference_journal(before["checkpoint_receipts_json"])
    pending = dict(owner.pending_changes)
    row = _native_memory_planned_sql_row(run, pending)
    if (row["status"] not in {"cancelled", "failed", "blocked", "unknown_external_effect", "cost_liability"}
            or row["revision"] != before["revision"] + 1
            or row["fencing_token"] not in {before["fencing_token"], before["fencing_token"] + 1}
            or row["checkpoint_context_json"] != before["checkpoint_context_json"]
            or row["effect_receipts_json"] != before["effect_receipts_json"]):
        raise ProductionWorkspaceReconciliationError("memory_negative_patch_invalid")
    candidate = _checked_memory_candidate_row(before)[1]["candidate"]
    refs = ([{"table": "workflow_run_states", "key": run.run_identity}] if current is None
            else current["payload"]["refs"])
    revision = 1 if current is None else current["payload"]["projection_revision"] + 1
    delta = _native_memory_selected_delta_preimage([], [(WRS_BY_RUN, run.run_identity,
        {name: row[name] for name in pending})], [])
    owner.header_budget.debit(len(_canonical(delta).encode("utf-8")))
    payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": None if original is None else _digest(original),
        "projection_revision": revision,
        "state": "unknown", "reason_code": ("original_projection_unavailable" if original is None
                                               else "current_source_unavailable"),
        "refs": refs, "absences": [], "rows": [], "rows_digest": None,
        "encoded_bytes": 0, "owner_operation_kind": candidate["method"],
        "owner_events": [], "selected_delta_digest": _digest(delta)}
    for _ in range(32):
        size = preflight_native_memory_reference_payload(payload, current=True)
        owner.header_budget.debit(size)
        if payload["encoded_bytes"] == size:
            break
        payload["encoded_bytes"] = size
    else:
        raise ProductionWorkspaceReconciliationError("memory_unknown_count_not_convergent")
    replacement = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "safe": True,
        "state_digest": _digest(payload), "payload": payload}
    journal = _splice_native_memory_current_checkpoint(before["checkpoint_receipts_json"], replacement)
    pending["checkpoint_receipts_json"] = journal
    after = _native_memory_planned_sql_row(run, pending)
    normalized_run, context, binding = _checked_memory_candidate_row(after)
    if original is None:
        _checked_memory_unknown_unverified_row(after, normalized_run, context, binding, replacement)
    else:
        # The real original owner supplied concrete pending binds. Validate
        # their prospective immutable shape without treating a numeric view
        # as a current SQL snapshot or authenticating historical M5 evidence.
        _checked_memory_original_bindings(after, normalized_run, context, binding, original, replacement)
    await reserve_native_memory_pending_run(db, run, pending, owner.header_budget, outputs=owner.outputs)
    statement = owner.statement.values(checkpoint_receipts_json=journal)
    plan = _NativeMemoryPublication(db, run, owner, statement,
        _native_memory_statement_signature(owner.statement), _native_memory_statement_signature(statement),
        before, after, certificate)
    await _validate_original_memory_negative_owner(db, run, owner)
    _MEMORY_PUBLICATIONS[id(plan)] = plan
    return plan


async def apply_native_memory_unknown(db, run, plan):
    """One armed original CAS; return its actual CursorResult unchanged."""
    from src.workflows.job_runtime import _validate_original_memory_negative_owner
    from src.memory.composition_headers import snapshot_reads, snapshot_reads_async, validate_composition_certificate, _certify_current_memory_snapshot
    from src.memory.universe import native_memory_universe
    if (type(plan) is not _NativeMemoryPublication or _MEMORY_PUBLICATIONS.get(id(plan)) is not plan
            or plan.seal is not _MEMORY_PUBLICATION_SEAL or plan.db is not db or plan.run is not run
            or plan.applied or plan.consumed):
        raise ProductionWorkspaceReconciliationError("memory_publication_plan_unavailable")
    guard = db.info.get("composition_guard")
    if type(guard) is not CompositionSessionGuard or guard.db is not db:
        raise ProductionWorkspaceReconciliationError("memory_publication_guard_missing")
    await _validate_original_memory_negative_owner(db, run, plan.original_owner)
    await validate_composition_certificate(db, plan.certificate)
    token = _ARMED_MEMORY_PUBLICATION.set(plan)
    try:
        async with snapshot_reads_async(db, plan.certificate):
            result = await db.execute(plan.statement)
        # Zero matches preserve the original caller's CAS failure behavior.
        if result.rowcount == 1 and not plan.consumed:
            raise ProductionWorkspaceReconciliationError("memory_publication_guard_missing")
        if result.rowcount not in {0, 1}:
            raise ProductionWorkspaceReconciliationError("memory_publication_cas_ambiguous")
        if result.rowcount == 1:
            await native_memory_universe(db)
            certificate = await _certify_current_memory_snapshot(db, plan.original_owner.header_budget)
            def read_back(connection):
                with snapshot_reads(certificate):
                    _, actual, _ = _native_memory_reference_row_on_connection(
                        connection, "workflow_run_states", plan.before["run_identity"])
                    if actual != plan.after:
                        raise ProductionWorkspaceReconciliationError("memory_publication_readback_changed")
                    _checked_memory_reference_row(connection, actual)
            await (await db.connection()).run_sync(read_back)
        plan.applied = True
        return result
    finally:
        _ARMED_MEMORY_PUBLICATION.reset(token)
        _MEMORY_PUBLICATIONS.pop(id(plan), None)


async def _preflight_native_memory_journal_headers(db, *, existing_references=(),
                                                  reserved_bytes=0, incoming_identity=None):
    """Bound the complete retained Memory job set before private journal reads.

    This is byte evidence only. The original admission/current owner must also
    certify core/v3, selected Memory rows, duplicate envelopes, actual upcoming
    effects and every Unknown replacement against the same remainder. Nothing
    here issues owner authority, mints a journal, or activates native Memory.
    ``reserved_bytes`` charges that owner's already established whole-closure
    reserve; a caller cannot use this helper as a publication permission.
    """
    from src.memory.header_bounds import (HeaderBoundsError, MAX_BYTES, MAX_ROWS,
        MEMORY_DESCRIPTORS, WRS_BY_RUN, preflight_exact_rows)
    from src.memory.universe import native_memory_universe
    if (type(existing_references) is not tuple or len(existing_references) > MAX_ROWS
            or type(reserved_bytes) is not int or not 0 <= reserved_bytes <= MAX_BYTES):
        raise HeaderBoundsError("memory_journal_reserve_invalid")
    references = set()
    for ref in existing_references:
        if (type(ref) is not tuple or len(ref) != 2 or type(ref[0]) is not str
                or ref[0] not in {*RETAINED_FIELDS, *MEMORY_DESCRIPTORS}
                or type(ref[1]) is not str or not ref[1]):
            raise HeaderBoundsError("memory_journal_reserve_invalid")
        try:
            bounded = len(ref[1].encode("utf-8")) <= 512
        except UnicodeEncodeError:
            bounded = False
        if not bounded:
            raise HeaderBoundsError("memory_journal_reserve_invalid")
        references.add(ref)
    if incoming_identity is not None:
        try:
            bounded = (type(incoming_identity) is str and bool(incoming_identity)
                       and len(incoming_identity.encode("utf-8")) <= 512)
        except UnicodeEncodeError:
            bounded = False
        if not bounded:
            raise HeaderBoundsError("memory_journal_reserve_invalid")
    identities = await native_memory_universe(db)
    references.update(("workflow_run_states", identity) for identity in identities)
    if incoming_identity is not None:
        references.add(("workflow_run_states", incoming_identity))
    if len(references) > MAX_ROWS:
        raise HeaderBoundsError("memory_closure_reference_bound")
    # One complete certificate is issued only after every existing job header
    # fits. Even a later oversized/foreign/unsealed/terminal row prevents an
    # earlier journal body from being fetched. New-row bytes are part of the
    # actual owner's prospective reserve, not guessed from an invented row.
    return await preflight_exact_rows(db, WRS_BY_RUN, identities,
                                      MAX_BYTES - reserved_bytes)


def _sql(connection, query, parameters=()):
    if hasattr(connection, "exec_driver_sql"):
        return connection.exec_driver_sql(query, parameters)
    return connection.execute(query, parameters)


NULL_SESSION_DDL_VERSION = "native-composition-null-session-ddl.v1"
NULL_SESSION_DDL_DESCRIPTOR = {
    "version": NULL_SESSION_DDL_VERSION,
    "column": ["continuity_task_id", "VARCHAR", 0, None, 0],
    "foreign_key": ["work_board_tasks", "continuity_task_id", "task_id", "NO ACTION", "NO ACTION", "NONE"],
    "index": ["ix_sessions_continuity_task_id", "continuity_task_id", 0, "c", 0, 0, "BINARY"],
}
NULL_SESSION_DDL_DIGEST = hashlib.sha256(json.dumps(
    NULL_SESSION_DDL_DESCRIPTOR, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _original_composition_metadata_rows(connection, query, parameters, columns, *, appearance):
    """Price original metadata widths before delivering any schema strings."""
    from src.memory.composition_headers import current_budget, _metadata_rows, _metadata_upper
    budget = current_budget()
    if budget is None:
        return list(_sql(connection, query, parameters))
    from src.memory.composition_headers import _original_metadata_census_sql
    scalar_query = _original_metadata_census_sql(query, columns)
    facts = _metadata_rows(connection, scalar_query, parameters, _header_budget=budget,
        upper=_metadata_upper(2, (20, 20, 20)), appearance=("composition-schema-width", *appearance))
    if (len(facts) != 1 or any(type(value) is not int for value in facts[0])
            or not 0 <= facts[0][0] <= 1359 or facts[0][1] < 0 or facts[0][2]):
        raise ProductionWorkspaceReconciliationError("composition_projection_schema_changed")
    count, widths, _ = facts[0]
    upper = 2 + count * (2 + max(0, len(columns) - 1)) + widths + max(0, count - 1)
    return _metadata_rows(connection, query, parameters, _header_budget=budget,
        upper=upper, appearance=("composition-schema", *appearance))


def validate_retained_table_schema(connection, table, *, error="composition_projection_schema_changed"):
    """Only the reviewed NULL-only Session addition is DDL-compatible with v3."""
    from src.memory.composition_headers import current_budget
    if current_budget() is not None and table != "sessions":
        from src.memory.composition_headers import _original_retained_schema_proved
        if _original_retained_schema_proved(connection, table, RETAINED_FIELDS[table]):
            return
        names = _original_composition_metadata_rows(connection,
            'SELECT name FROM pragma_table_info(?) LIMIT ?', (table, len(RETAINED_FIELDS[table]) + 1),
            ("name",), appearance=(table, "columns"))
        if {row[0] for row in names} != set(RETAINED_FIELDS[table]):
            raise ProductionWorkspaceReconciliationError(error)
        return
    columns = _original_composition_metadata_rows(connection,
        'SELECT cid,name,type,"notnull",dflt_value,pk FROM pragma_table_info(?) LIMIT 1360',
        (table,), ("cid", "name", "type", "notnull", "dflt_value", "pk"), appearance=(table, "columns"))
    actual = {row[1] for row in columns}
    expected = set(RETAINED_FIELDS[table])
    if table != "sessions":
        if actual != expected:
            raise ProductionWorkspaceReconciliationError(error)
        return
    index_name = "ix_sessions_continuity_task_id"
    named = _original_composition_metadata_rows(connection,
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=? LIMIT 1360", (index_name,),
        ("tbl_name",), appearance=(table, "continuity-index-owner"))
    if actual == expected:
        if named:
            raise ProductionWorkspaceReconciliationError(error)
        return
    if actual != expected | {"continuity_task_id"}:
        raise ProductionWorkspaceReconciliationError(error)
    column = next(row for row in columns if row[1] == "continuity_task_id")
    if tuple(column[1:]) != tuple(NULL_SESSION_DDL_DESCRIPTOR["column"]):
        raise ProductionWorkspaceReconciliationError(error)
    all_foreign = [tuple(row) for row in _original_composition_metadata_rows(connection,
        'SELECT id,seq,"table","from","to",on_update,on_delete,match FROM pragma_foreign_key_list(?) LIMIT 1360',
        (table,), ("id", "seq", "table", "from", "to", "on_update", "on_delete", "match"),
        appearance=(table, "foreign-keys"))]
    foreign = [row for row in all_foreign if row[3] == "continuity_task_id"]
    if (len(foreign) != 1 or foreign[0][1] != 0
        or foreign[0][2:] != tuple(NULL_SESSION_DDL_DESCRIPTOR["foreign_key"])
        or sum(row[0] == foreign[0][0] for row in all_foreign) != 1):
        raise ProductionWorkspaceReconciliationError(error)
    if len(named) != 1 or named[0][0] != "sessions":
        raise ProductionWorkspaceReconciliationError(error)
    indexes = [row for row in _original_composition_metadata_rows(connection,
        'SELECT seq,name,"unique",origin,partial FROM pragma_index_list(?) LIMIT 1360', (table,),
        ("seq", "name", "unique", "origin", "partial"), appearance=(table, "indexes")) if row[1] == index_name]
    if len(indexes) != 1 or tuple(indexes[0][2:]) != (0, "c", 0):
        raise ProductionWorkspaceReconciliationError(error)
    info = [tuple(row) for row in _original_composition_metadata_rows(connection,
        'SELECT seqno,cid,name FROM pragma_index_info(?) LIMIT 1360', (index_name,),
        ("seqno", "cid", "name"), appearance=(table, "continuity-index-info"))]
    keys = [tuple(row) for row in _original_composition_metadata_rows(connection,
        'SELECT seqno,cid,name,desc,coll,key FROM pragma_index_xinfo(?) LIMIT 1360', (index_name,),
        ("seqno", "cid", "name", "desc", "coll", "key"), appearance=(table, "continuity-index-parts")) if row[5] == 1]
    if info != [(0, column[0], "continuity_task_id")] or keys != [(0, column[0], "continuity_task_id", 0, "BINARY", 1)]:
        raise ProductionWorkspaceReconciliationError(error)


def _composition_address(table, key):
    if table == "work_board_events":
        if type(key) is int and -(2**63) <= key < 2**63:
            return str(key)
        if type(key) is str:
            try:
                value = int(key)
            except ValueError:
                pass
            else:
                if -(2**63) <= value < 2**63 and str(value) == key:
                    return key
        raise ProductionWorkspaceReconciliationError("composition_reference_invalid")
    return key


def _composition_row(connection, table, key):
    key = _composition_address(table, key)
    if table not in RETAINED_FIELDS or type(key) is not str or len(key.encode()) > 512:
        raise ProductionWorkspaceReconciliationError("composition_reference_invalid")
    from src.memory.composition_headers import charge_row
    charge_row(connection, table, key)
    columns = RETAINED_FIELDS[table]
    names = ",".join('"' + field + '"' for field in columns)
    result = _sql(connection, f'SELECT {names} FROM "{table}" WHERE "{COMPOSITION_KEYS[table]}"=?', ((int(key) if table == "work_board_events" else key),)).fetchall()
    if len(result) != 1:
        raise ProductionWorkspaceReconciliationError("composition_canonical_row_missing")
    if table == "sessions":
        # The merged continuity FK is outside the reviewed v3 native closure.
        # Preserve the frozen tuple; never drop a live task link on restoration.
        actual_fields = {row[1] for row in _sql(connection, 'PRAGMA table_info("sessions")')}
        if "continuity_task_id" in actual_fields:
            linked = _sql(connection, 'SELECT "continuity_task_id" FROM "sessions" WHERE "id"=?', (key,)).fetchone()
            if linked is None or linked[0] is not None:
                raise ProductionWorkspaceReconciliationError("composition_session_continuity_unsupported")
    return dict(zip(columns, result[0]))


def checked_turn_family(row):
    """Pure protected payload/binding check for ORM and retained SQL rows."""
    from src.agent.native_turn_family import FAMILY_CHECKPOINT_ID, validate_family_binding
    from src.workflows.job_runtime import _digest
    from src.runtime_plugins.dispatch import _receipt_witness
    entries = json.loads(row["checkpoint_receipts_json"] or "[]")
    if type(entries) is not list:
        raise ProductionWorkspaceReconciliationError("composition_turn_family_history_invalid")
    claims = [item for item in entries if type(item) is dict and str(item.get("checkpoint_id", "")).startswith("runtime-service-invocation:")]
    if row["attempt_count"] > 0:
        if len(claims) != 1:
            raise ProductionWorkspaceReconciliationError("composition_turn_family_claim_missing")
        claim = _receipt_witness(claims[0])
        if any(claim[field] != row[column] for field, column in (
                ("invocation_ref", "run_identity"), ("input_digest", "input_digest"),
                ("authority_digest", "authority_digest"), ("run_fingerprint", "run_fingerprint"))):
            raise ProductionWorkspaceReconciliationError("composition_turn_family_claim_changed")
    families = [item for item in entries if type(item) is dict and item.get("checkpoint_id") == FAMILY_CHECKPOINT_ID]
    if not families:
        if any(type(item) is dict and item.get("checkpoint_id") in
               {"conversation:assistant-message", "conversation:controlled-outcome"} for item in entries):
            raise ProductionWorkspaceReconciliationError("composition_turn_family_missing")
        return None  # Claimed but not yet initialized: pending, no publication authority.
    if (len(families) != 1 or set(families[0]) != {"checkpoint_id", "state_digest", "safe", "payload"}
        or families[0]["safe"] is not True or families[0]["state_digest"] != _digest(families[0]["payload"])):
        raise ProductionWorkspaceReconciliationError("composition_turn_family_receipt_invalid")
    if len(claims) != 1:
        raise ProductionWorkspaceReconciliationError("composition_turn_family_claim_invalid")
    _receipt_witness(claims[0])
    return validate_family_binding(families[0]["payload"], SimpleNamespace(**row), claim_payload=claims[0]["payload"])


def encode_turn_float(value):
    if type(value) is not float or not math.isfinite(value):
        raise ProductionWorkspaceReconciliationError("composition_extension_float_invalid")
    return {"type": "float64", "hex": value.hex()}


def decode_turn_float(value):
    if type(value) is not dict or set(value) != {"type", "hex"} or value["type"] != "float64" or type(value["hex"]) is not str:
        raise ProductionWorkspaceReconciliationError("composition_extension_float_invalid")
    try:
        result = float.fromhex(value["hex"])
    except (ValueError, OverflowError) as exc:
        raise ProductionWorkspaceReconciliationError("composition_extension_float_invalid") from exc
    if encode_turn_float(result) != value:
        raise ProductionWorkspaceReconciliationError("composition_extension_float_noncanonical")
    return result


def composition_row_digest(table, key, row):
    from src.memory.composition_headers import current_budget
    from src.workspace.production import _publication_json_upper as width
    payer = current_budget()
    if payer is not None:
        fields = NATIVE_EXTENSION_FIELDS.get(table, COMPOSITION_FIELDS.get(table))
        if fields is None or len(row) != len(fields) or any(field not in row for field in fields):
            raise ProductionWorkspaceReconciliationError("composition_scalar_schema_invalid")
        # Pair-list JSON adds two brackets per field relative to the original
        # object header. Float64's accepted finite hex representation fits 128
        # logical JSON bytes; no float codec/output is made to measure it.
        row_upper = 2 + sum(width(field) + 1 + (128 if table == "memory_episodes"
            and field in {"salience", "confidence"} else width(row[field]))
            for field in fields) + max(0, len(fields) - 1)
        for appearance, upper in _original_native_row_digest_entries(table, key, row_upper):
            payer.debit(upper, appearance=appearance)
    if table in NATIVE_EXTENSION_FIELDS:
        fields = NATIVE_EXTENSION_FIELDS[table]
        if set(row) != set(fields):
            raise ProductionWorkspaceReconciliationError("composition_extension_schema_invalid")
        values = []
        for field in fields:
            value = row[field]
            if table == "memory_episodes" and field in {"salience", "confidence"}:
                value = encode_turn_float(value)
            elif value is not None and type(value) not in {str, int}:
                raise ProductionWorkspaceReconciliationError("composition_extension_scalar_invalid")
            values.append([field, value])
        encoded = json.dumps([NATIVE_EXTENSION_VERSION, NATIVE_EXTENSION_DIGEST, table, key, values],
            ensure_ascii=False, separators=(",", ":")).encode()
        return hashlib.sha256(b"seraph-continuity-extension-row-v1\0" + encoded).hexdigest()
    fields = COMPOSITION_FIELDS[table]
    if set(row) != set(fields) or any(value is not None and type(value) not in {str, int} for value in row.values()):
        raise ProductionWorkspaceReconciliationError("composition_scalar_schema_invalid")
    encoded = json.dumps([COMPOSITION_SCHEMA, table, key, [[field, row[field]] for field in fields]],
                         ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(b"seraph-continuity-row-v1\0" + encoded).hexdigest()


def _original_native_row_digest_entries(table, key, row_upper):
    """Original row codec capacity from an authentic numeric object header."""
    from src.workspace.production import _publication_json_upper as width
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    fields = NATIVE_EXTENSION_FIELDS.get(table, COMPOSITION_FIELDS.get(table))
    if (fields is None or type(key) is not str or type(row_upper) is not int
            or not 0 < row_upper <= MAX_BYTES):
        raise HeaderBoundsError("programme_native_row_numeric_unavailable")
    values = row_upper + 2 * len(fields)
    extension = table in NATIVE_EXTENSION_FIELDS
    prefix = b"seraph-continuity-extension-row-v1\0" if extension else b"seraph-continuity-row-v1\0"
    payload = 2 + width(table) + width(key) + values + 2
    payload += (width(NATIVE_EXTENSION_VERSION) + width(NATIVE_EXTENSION_DIGEST) + 2
        if extension else width(COMPOSITION_SCHEMA) + 1)
    field_keys = 2 + sum(width(field) for field in fields) + max(0, len(fields) - 1)
    entries = tuple((("native-row-digest", table, key, stage), upper) for stage, upper in (
        ("row-field-set", field_keys), ("schema-field-set", field_keys),
        ("field-pairs", values), ("payload", payload), ("json", payload),
        ("utf8", payload), ("hash-preimage", len(prefix) + payload),
        ("hash-input", len(prefix) + payload), ("digest-return", 66)))
    if table == "memory_episodes":
        entries += tuple((("native-row-digest", table, key, stage, field), upper)
            for field in ("salience", "confidence")
            for stage, upper in (("float-hex", 64), ("float-codec", 128)))
    return entries


def _original_native_closure_projected_entries(trace, *, constructor, publication_widths):
    """Replace only source-owned native codecs for one typed future constructor.

    Keep every genuine retained-root event. Only numeric row/header/component
    bounds are consumed; this never constructs a future row, hash or witness.
    """
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    from src.workspace.production import _publication_json_upper as width
    if (type(constructor) is not MappingProxyType or type(publication_widths) is not MappingProxyType
            or type(constructor.get("job_id")) is not str
            or type(constructor.get("row_header_bytes")) is not int):
        raise HeaderBoundsError("programme_native_closure_numeric_unavailable")
    job_id = constructor["job_id"]
    if (not job_id.startswith("goal-discovery:") or len(job_id) != len("goal-discovery:") + 32
            or any(char not in "0123456789abcdef" for char in job_id[len("goal-discovery:"):])
            or ("persisted_queue_only" in constructor and type(constructor["persisted_queue_only"]) is not bool)):
        raise HeaderBoundsError("programme_native_closure_numeric_unavailable")
    persisted = constructor.get("persisted_queue_only") is True
    required_tail = {"closure-prefix", "visited-sort", "table-counts", "inventory-list",
        "inventory-json", "inventory-utf8", "inventory-hash", "inventory-digest-return", "role-counts",
        "relations-sort", "relations-json", "relations-utf8", "relations-hash", "artifacts-sort",
        "artifacts-json", "artifacts-utf8", "artifacts-hash", "closure-digest-return",
        "relations-digest-return", "artifacts-digest-return", "result", "visited-return"}
    actual_tail = {appearance[1] for appearance, upper in trace
        if type(appearance) is tuple and len(appearance) == 2 and appearance[0] == "native-closure-tail"}
    actual_envelope = {appearance[1] for appearance, upper in trace
        if type(appearance) is tuple and len(appearance) == 2 and appearance[0] == "programme-snapshot-envelope"}
    candidate_codecs = sum(type(appearance) is tuple
        and appearance[:3] == ("native-row-digest", "workflow_run_states", job_id)
        for appearance, upper in trace)
    if (not required_tail <= actual_tail
            or actual_envelope != {"construction", "return", "leaves-set", "members-union"}
            or candidate_codecs != (10 if persisted else 0)):
        raise HeaderBoundsError("programme_native_closure_numeric_unavailable")
    needed = ("native_bytes", "target_bytes", "members_bytes")
    if any(type(publication_widths.get(name)) is not int
            or not 0 <= publication_widths[name] <= MAX_BYTES for name in needed):
        raise HeaderBoundsError("programme_native_closure_numeric_unavailable")
    key_width = width(("workflow_run_states", job_id))
    # A discovery constructor's legacy session FK is NULL, so it adds no native
    # role/file/inventory member. Programme relations/files have their own owner.
    increment = 0 if persisted else key_width + 1
    result = []
    for appearance, upper in trace:
        if (type(upper) is not int or not 0 <= upper <= MAX_BYTES
                or (appearance is not None and type(appearance) is not tuple)):
            raise HeaderBoundsError("programme_native_closure_numeric_unavailable")
        if type(appearance) is tuple and appearance[:3] == ("native-row-digest", "workflow_run_states", job_id):
            continue
        if type(appearance) is tuple and appearance[:1] == ("native-closure-tail",):
            stage = appearance[1]
            if stage in {"visited-sort", "visited-return"}:
                upper += increment
            elif stage == "table-counts":
                upper += 0 if persisted else width("workflow_run_states") + 22
            elif stage == "result":
                upper = publication_widths["native_bytes"]
        elif type(appearance) is tuple and appearance[:1] == ("programme-snapshot-envelope",):
            upper = (publication_widths["members_bytes"] if appearance[1] in {"leaves-set", "members-union"}
                else publication_widths["target_bytes"])
        result.append((appearance, upper))
    result.extend(_original_native_row_digest_entries("workflow_run_states", job_id,
        constructor["row_header_bytes"]))
    if not persisted:
        for _ in range(2):
            result.append((("body", "workflow_run_states", job_id), constructor["row_header_bytes"]))
        # One new sort-key UTF8 copy and each real length-prefixed hash update.
        result.append((("native-closure-tail", "key-utf8", "workflow_run_states", job_id),
            sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in job_id)))
        for field, value_upper in (("table", width("workflow_run_states")), ("key", width(job_id)), ("leaf", 64)):
            for stage, upper in (("utf8", value_upper), ("length-prefix", 8), ("hash-prefix", 8), ("hash-value", value_upper)):
                result.append((("native-closure-tail", stage, "workflow_run_states", job_id, field), upper))
    return tuple(result)


def _checked_memory_candidate_row(row):
    """Original input encodings and immutable binding, without a Source grant."""
    from src.runtime_plugins.memory_producer import memory_context
    from src.runtime_plugins.dispatch import _receipt_witness, _ms
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    from src.workflows.job_runtime import _canonical, _digest, _safe_durable_inputs, _composition_fingerprint
    try:
        run = SimpleNamespace(**row)
        run.deadline_at = datetime.fromisoformat(row["deadline_at"])
        context = memory_context(run)
        candidate = context["candidate"]
        from src.memory.composition_headers import current_budget
        payer = current_budget()
        if payer is not None:
            from src.workflows.inference_accounting import _original_native_binding_numeric_entries, _accounting_prepay
            original_text = row["composition_binding_json"]
            text_bytes = sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in original_text)
            _accounting_prepay(payer, _original_native_binding_numeric_entries(row["run_identity"], text_bytes))
        binding = RuntimeCompositionBinding.from_json(row["composition_binding_json"])
        _, original_inputs = _safe_durable_inputs(candidate)
        authority = strict_json_loads(row["declared_authority_json"])
        if (strict_json_loads(row["arguments_json"]) != original_inputs
                or row["arguments_json"] != _canonical(original_inputs)
                or type(authority) is not dict or set(authority) != {"principal", "owner_kind", "session_id", "grants"}
                or authority["principal"] != candidate["operator_principal_id"]
                or authority["owner_kind"] != "user"
                or authority["session_id"] != candidate["operator_session_id"]
                or type(authority["grants"]) is not list
                or any(type(grant) is not str for grant in authority["grants"])
                or authority["grants"] != sorted(set(authority["grants"]))
                or row["authority_digest"] != _digest(authority)
                or row["run_fingerprint"] != _composition_fingerprint(SimpleNamespace(
                    run_fingerprint=_digest({"candidate": context["candidate_digest"],
                                             "binding": binding.binding_digest}),
                    composition_binding=binding), context["candidate_digest"])):
            raise ValueError("memory_preoriginal_inputs_changed")
        return run, context, binding
    except (ValueError, TypeError, KeyError, AttributeError, HeaderBoundsError) as error:
        raise ProductionWorkspaceReconciliationError("composition_native_memory_preoriginal_invalid") from error


def _checked_memory_claim_row(row, run, context, binding, *, unverified_unknown=False):
    """Claim byte/binding inspection only; never a claim issuer or transfer."""
    from src.runtime_plugins.dispatch import _receipt_witness, _ms
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    try:
        entries = strict_json_loads(row["checkpoint_receipts_json"])
        claims = [entry for entry in entries if type(entry) is dict
                  and str(entry.get("checkpoint_id", "")).startswith("runtime-service-invocation:")]
        if type(row["attempt_count"]) is not int or not 0 <= row["attempt_count"] <= 1:
            raise ValueError("memory_preoriginal_claim_changed")
        if row["attempt_count"] == 0:
            if claims or row["status"] == "running" or row["lease_owner"] is not None:
                raise ValueError("memory_preoriginal_claim_changed")
        else:
            if len(claims) != 1:
                raise ValueError("memory_preoriginal_claim_changed")
            if set(claims[0]) != {"checkpoint_id", "state_digest", "safe", "payload"}:
                raise ValueError("memory_preoriginal_claim_changed")
            claim = _receipt_witness(claims[0])
            from src.memory.composition_headers import current_budget
            payer = current_budget()
            if payer is not None:
                from src.workflows.inference_accounting import _original_native_binding_numeric_entries, _accounting_prepay
                original_text = row["composition_binding_json"]
                text_bytes = sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in original_text)
                _accounting_prepay(payer, _original_native_binding_numeric_entries(row["run_identity"], text_bytes))
            binding = RuntimeCompositionBinding.from_json(row["composition_binding_json"])
            if (any(claim[field] != row[column] for field, column in (
                    ("invocation_ref", "run_identity"), ("input_digest", "input_digest"),
                    ("authority_digest", "authority_digest"), ("run_fingerprint", "run_fingerprint"),
                    ("attempt_count", "attempt_count")))
                    or (not unverified_unknown and claim["fencing_token"] != row["fencing_token"])
                    or (unverified_unknown and not claim["fencing_token"] <= row["fencing_token"] <= claim["fencing_token"] + 1)
                    or claim["origin_method"] != binding.origin_method
                    or claim["native_branch"] != binding.native_branch
                    or claim["allowed_child_methods"] != binding.allowed_child_methods
                    or claim["composition_binding_digest"] != binding.binding_digest
                    or claim["package_digest"] != binding.host_package_digest
                    or claim["host_composition_digest"] != binding.host_composition_digest
                    or claim["host_boot_nonce"] != context["candidate"]["host_boot_nonce"]
                    or claim["original_deadline_at"] != _ms(run.deadline_at)
                    or (row["status"] == "running" and claim["lease_owner"] != row["lease_owner"])):
                raise ValueError("memory_preoriginal_claim_changed")
        return None if not claims else claim
    except (ValueError, TypeError, KeyError, AttributeError, HeaderBoundsError) as error:
        raise ProductionWorkspaceReconciliationError("composition_native_memory_preoriginal_invalid") from error


def _checked_preoriginal_memory_row(row):
    """Bounded read-only pre-effect retention; never execution permission."""
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    try:
        run, context, binding = _checked_memory_candidate_row(row)
        original, current = preflight_native_memory_reference_journal(row["checkpoint_receipts_json"])
        if (original is not None or current is not None or context["result"] is not None
                or row["status"] not in {"accepted", "queued", "running", "awaiting_approval", "paused", "blocked",
                                        "cancelled", "failed", "unknown_external_effect", "cost_liability"}
                or strict_json_loads(row["effect_receipts_json"]) != []):
            raise ValueError("memory_preoriginal_state_changed")
        _checked_memory_claim_row(row, run, context, binding)
        return context["candidate"]
    except (ValueError, TypeError, KeyError, AttributeError, HeaderBoundsError) as error:
        raise ProductionWorkspaceReconciliationError("composition_native_memory_preoriginal_invalid") from error


def _checked_memory_unknown_unverified_row(row, run, context, binding, current):
    """Retain opaque uncertainty; checksum/counter never proves a past owner."""
    from src.memory.header_bounds import strict_json_loads
    payload = current["payload"]
    if (payload["state"] != "unknown" or payload["reason_code"] != "original_projection_unavailable"
            or payload["original_checkpoint_digest"] is not None or context["result"] is not None
            or strict_json_loads(row["effect_receipts_json"]) != []
            or row["status"] not in {"cancelled", "failed", "blocked", "unknown_external_effect", "cost_liability"}
            or payload["owner_events"] != [] or payload["absences"] != []):
        raise ValueError("memory_unknown_unverified_shape_changed")
    _checked_memory_claim_row(row, run, context, binding, unverified_unknown=True)
    return context["candidate"], None


def _native_memory_reference_row_on_connection(connection, table, key):
    """Read one current certified raw reference tuple, never Source authority."""
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS, MEMORY_DESCRIPTORS, HeaderBoundsError
    from src.memory.composition_headers import charge_row, current_budget
    descriptor = COMPOSITION_DESCRIPTORS.get(table)
    if (current_budget() is None or descriptor is None
            or table not in {*RETAINED_FIELDS, *MEMORY_DESCRIPTORS}):
        raise HeaderBoundsError("memory_current_bound_not_certified")
    charge_row(connection, table, key)
    columns = ",".join('"' + name + '"' for name in descriptor.columns)
    parameter = int(_composition_address(table, key)) if table == "work_board_events" else key
    records = list(_sql(connection, f'SELECT {columns} FROM "{table}" WHERE "{descriptor.key}"=? LIMIT 2', (parameter,)))
    if len(records) != 1:
        raise HeaderBoundsError("memory_reference_row_missing")
    row = dict(zip(descriptor.columns, records[0]))
    return descriptor, row, _native_memory_row_bytes(descriptor, key, row)


def _programme_reference_row_on_connection(connection, table, key):
    """Named common33 programme provenance, using unchanged raw tuple bytes."""
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS, HeaderBoundsError
    from src.memory.composition_headers import charge_row, current_budget, _CURRENT, _validate
    if current_budget() is None or table not in {"workflow_run_states", "goals", "operator_sessions"}:
        raise HeaderBoundsError("programme_current_bound_not_certified")
    descriptor = COMPOSITION_DESCRIPTORS[table]
    certificate = _CURRENT.get()
    _validate(connection, certificate)
    upper = certificate.rows[(table, key)][1]
    for appearance in ("dict-copy", "encoded-copy"):
        certificate.budget.debit(upper, appearance=("programme-row-output", appearance, table, key))
    charge_row(connection, table, key)
    columns = ",".join('"' + name + '"' for name in descriptor.columns)
    records = list(_sql(connection, f'SELECT {columns} FROM "{table}" WHERE "{descriptor.key}"=? LIMIT 2', (key,)))
    if len(records) != 1:
        raise HeaderBoundsError("programme_reference_row_missing")
    row = dict(zip(descriptor.columns, records[0]))
    return descriptor, row, _native_memory_row_bytes(descriptor, key, row)


def _checked_memory_original_bindings(row, run, context, binding, original, current):
    """Immutable cold evidence shape only; no SQL, M5 MAC or owner authority."""
    from dataclasses import asdict
    from src.runtime_plugins.memory_producer import MemoryOwnerEffect
    from src.workflows.job_runtime import _digest
    candidate, payload = context["candidate"], current["payload"]
    original_payload = original["payload"]
    result = context["result"]
    if result is None or type(result["effect"]) is not dict:
        raise ValueError("memory_original_effect_missing")
    effect = MemoryOwnerEffect(**result["effect"])
    if (set(result["effect"]) != set(asdict(effect)) or effect.status not in {"succeeded", "blocked"}
            or effect.method != candidate["method"]
            or effect.candidate_digest != context["candidate_digest"]
            or effect.audit_event_id is None
            or original_payload["invocation_ref"] != row["run_identity"]
            or original_payload["claim_ref"] != result["claim_ref"]
            or original_payload["candidate_digest"] != context["candidate_digest"]
            or original_payload["composition_binding_digest"] != binding.binding_digest
            or original_payload["method"] != candidate["method"]
            or original_payload["owner_principal_id"] != candidate["operator_principal_id"]
            or original_payload["operator_session_id"] != candidate["operator_session_id"]
            or original_payload["original_deadline"] != candidate["original_deadline"]
            or original_payload["source_binding_digest"] != _digest(candidate.get("source"))
            or original_payload["original_effect_digest"] != _digest(asdict(effect))
            or payload["original_checkpoint_digest"] != _digest(original)
            or result["retention"]["original_checkpoint_digest"] != _digest(original)
            or payload["refs"] != original_payload["refs"]):
        raise ValueError("memory_original_binding_changed")
    claim = _checked_memory_claim_row(row, run, context, binding,
                                      unverified_unknown=payload["state"] == "unknown")
    if claim is None or claim["claim_ref"] != result["claim_ref"]:
        raise ValueError("memory_original_claim_changed")
    if (claim["input_digest"] != context["candidate_digest"]
            or claim["authority_digest"] != row["authority_digest"]
            or claim["run_fingerprint"] != row["run_fingerprint"]
            or claim["composition_binding_digest"] != binding.binding_digest
            or claim["host_boot_nonce"] != candidate["host_boot_nonce"]):
        raise ValueError("memory_original_claim_changed")
    audit_ref = original_payload["original_audit"]["ref"]
    if audit_ref != {"table": "audit_events", "key": effect.audit_event_id}:
        raise ValueError("memory_original_audit_changed")
    return effect, audit_ref


def _checked_memory_reference_row(connection, row):
    """Source-independent current evidence inspection; never publication permission."""
    from src.memory.header_bounds import HeaderBoundsError, WRS_BY_RUN, strict_json_loads
    from src.memory.composition_headers import charge_row, current_budget
    try:
        budget = current_budget()
        if budget is None:
            raise HeaderBoundsError("memory_current_bound_not_certified")
        charge_row(connection, "workflow_run_states", row["run_identity"])
        run, context, binding = _checked_memory_candidate_row(row)
        original, current = preflight_native_memory_reference_journal(row["checkpoint_receipts_json"])
        if original is None and current is None:
            return _checked_preoriginal_memory_row(row), None
        if current is None:
            raise ValueError("memory_current_projection_missing")
        candidate, payload = context["candidate"], current["payload"]
        if ((original is None or payload["state"] != "unknown")
                and payload["owner_operation_kind"] != candidate["method"]):
            raise ValueError("memory_current_operation_changed")
        own_ref = {"table": "workflow_run_states", "key": row["run_identity"]}
        if own_ref not in payload["refs"]:
            raise ValueError("memory_current_own_reference_missing")
        if original is None:
            return _checked_memory_unknown_unverified_row(row, run, context, binding, current)
        effect, audit_ref = _checked_memory_original_bindings(row, run, context, binding, original, current)
        if payload["state"] == "unknown":
            # The fixed parser and immutable native bindings permit retaining
            # opaque uncertainty. They do not authenticate the old M5 effect,
            # an intervening owner, or a historical CAS. In particular, the
            # closed operation value can describe a different later M5 owner.
            # Only the validated branch below reports verified audit evidence.
            return candidate, None
        _, audit, audit_bytes = _native_memory_reference_row_on_connection(connection, "audit_events", effect.audit_event_id)
        audit_digest = hashlib.sha256(audit_bytes).hexdigest()
        details = strict_json_loads(audit["details_json"])
        expected_types = ({"memory_learning_proposed"} if candidate["method"] == "memory.propose" else
            {"memory_forgotten"} if candidate["method"] == "memory.forget" else
            {"memory_learning_accepted", "memory_corrected", "memory_learning_rejected", "memory_learning_expired",
             "memory_learning_rolled_back"})
        if (audit_digest != original["payload"]["original_audit"]["tuple_digest"]
                or audit["actor"] != candidate["operator_principal_id"]
                or audit["session_id"] != candidate["operator_session_id"]
                or audit["tool_name"] != "memory_control" or audit["policy_mode"] != "operator_controlled"
                or audit["event_type"] not in expected_types
                or type(details) is not dict
                or (effect.proposal_id is not None and details.get("proposal_id") != effect.proposal_id)
                or (candidate["method"] == "memory.forget" and details.get("memory_id") != effect.record_id)):
            raise ValueError("memory_original_audit_changed")
        if payload["absences"]:
            raise ValueError("memory_current_absence_unavailable")
        if _native_memory_current_rows_digest(payload["rows"], row["run_identity"]) != payload["rows_digest"]:
            raise ValueError("memory_current_aggregate_changed")
        raw_rows = {}
        for witness in payload["rows"]:
            table, key = witness["ref"]["table"], witness["ref"]["key"]
            if witness["ref"] == own_ref:
                descriptor, actual = WRS_BY_RUN, row
                raw_bytes = _native_memory_row_bytes(descriptor, key, actual)
                budget.debit(len(raw_bytes))
                projected = dict(actual, checkpoint_receipts_json=_native_memory_own_journal_projection(
                    actual["checkpoint_receipts_json"], key))
                digest_bytes = _native_memory_row_bytes(descriptor, key, projected)
            else:
                descriptor, actual, raw_bytes = _native_memory_reference_row_on_connection(connection, table, key)
                digest_bytes = raw_bytes
            if (len(raw_bytes) != witness["encoded_bytes"]
                    or hashlib.sha256(digest_bytes).hexdigest() != witness["tuple_digest"]):
                raise ValueError("memory_current_row_changed")
            raw_rows[(table, key)] = actual
        from src.memory.repository import _m5_receipt_integrity_matches, _m5_receipt_binding_matches, _m5_selection_binding_matches
        for (table, key), actual in raw_rows.items():
            if table == "work_board_decision_receipts":
                proposal = raw_rows.get(("memory_proposals", actual["source_proposal_id"]))
                if not _m5_receipt_integrity_matches(actual) or not _m5_receipt_binding_matches(actual, proposal):
                    raise ValueError("memory_current_mac_changed")
            if table == "memory_proposals" and actual["status"] == "accepted":
                memory = raw_rows.get(("memories", actual["accepted_memory_id"]))
                if memory is None:
                    raise ValueError("memory_current_accepted_memory_missing")
                from src.memory.m5 import m5_text_digest
                provenance = strict_json_loads(memory["metadata_json"]).get("work_board_provenance")
                if (m5_text_digest(memory["content"]) != actual["accepted_memory_content_digest"]
                        or not _m5_selection_binding_matches(provenance, proposal_id=actual["proposal_id"],
                            accepted_content_digest=actual["accepted_memory_content_digest"],
                            decision_effect=actual["decision_effect"], memory_scope=strict_json_loads(actual["memory_scope_json"]),
                            source_binding=actual, corrects_memory_id=actual["corrects_memory_id"],
                            recovered_from_proposal_id=actual["recovered_from_proposal_id"])):
                    raise ValueError("memory_current_mac_changed")
        return candidate, (effect.audit_event_id, audit_digest)
    except (ValueError, TypeError, KeyError, AttributeError, HeaderBoundsError) as error:
        raise ProductionWorkspaceReconciliationError("composition_native_memory_reference_invalid") from error


PROGRAMME_MANIFEST_VERSION = "native-composition-programme.v1"
PROGRAMME_MANIFEST_DIGEST = "9ef9f8673209976ebd8c47df817509d1b637cf75a6000371d130e06a8ef4426c"
COMPOSITION_ENVELOPE_SCHEMA = "composition-continuity-envelope.v2"


def _programme_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


def _programme_digest(domain, value):
    return hashlib.sha256(domain + _programme_json(value)).hexdigest()


def _programme_aggregate(domain):
    digest = hashlib.sha256(domain)
    _programme_length_prefix(digest, PROGRAMME_MANIFEST_VERSION)
    _programme_length_prefix(digest, PROGRAMME_MANIFEST_DIGEST)
    return digest


def _programme_length_prefix(digest, value):
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _programme_identity_leaf(key, row):
    # Identity alone has this new explicit codec. Goal/issuer/native leaves
    # continue to use their original accepted codecs byte for byte.
    fields = ("id", "created_at", "revoked_at")
    if (type(key) is not str or len(row) != 3 or row[0] != key
            or any(value is not None and type(value) not in {str, int} for value in row)
            or any(type(value) is int and not -(2**63) <= value < 2**63 for value in row)):
        raise ProductionWorkspaceReconciliationError("programme_identity_raw_invalid")
    payload = json.dumps([PROGRAMME_MANIFEST_VERSION, PROGRAMME_MANIFEST_DIGEST,
        "operator_identities", key, [[name, value] for name, value in zip(fields, row)]],
        ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(b"seraph-continuity-programme-identity-row-v1\0" + payload).hexdigest()


def _programme_envelope(native, programme, transition_ref):
    validate_composition_progression(native, native)
    envelope = {"schema_version": 2, "projection_schema": COMPOSITION_ENVELOPE_SCHEMA,
        "native": native, "programme": programme, "transition_ref": transition_ref}
    _validate_programme_envelope(envelope)
    return envelope


def _validate_programme_envelope(value):
    import re
    def digest(item):
        return type(item) is str and re.fullmatch(r"[0-9a-f]{64}", item) is not None
    if (type(value) is not dict or set(value) != {"schema_version", "projection_schema",
            "native", "programme", "transition_ref"} or type(value["schema_version"]) is not int
            or value["schema_version"] != 2 or value["projection_schema"] != COMPOSITION_ENVELOPE_SCHEMA
            or not (value["transition_ref"] is None or digest(value["transition_ref"]))
            or type(value["native"]) is not dict or value["native"].get("schema_version") != 1):
        raise ProductionWorkspaceReconciliationError("composition_envelope_invalid")
    validate_composition_progression(value["native"], value["native"])
    component = value["programme"]
    if component is None:
        return
    if (type(component) is not dict or set(component) != {"manifest_version", "manifest_digest",
            "row_counts", "closure_digest", "relation_counts", "relation_digest", "artifact_count",
            "artifact_manifest_digest"} or component["manifest_version"] != PROGRAMME_MANIFEST_VERSION
            or component["manifest_digest"] != PROGRAMME_MANIFEST_DIGEST
            or any(not digest(component[key]) for key in (
                "closure_digest", "relation_digest", "artifact_manifest_digest"))
            or type(component["artifact_count"]) is not int or not 0 <= component["artifact_count"] <= 128):
        raise ProductionWorkspaceReconciliationError("composition_programme_component_invalid")
    allowed = ({"goals", "operator_sessions", "operator_identities"},
        {"programme_goal", "goal_parent_fk", "programme_issuer_provenance", "programme_owner_identity"})
    for name, keys in zip(("row_counts", "relation_counts"), allowed):
        counts = component[name]
        if (type(counts) is not dict or not set(counts) <= keys
                or any(type(count) is not int or not 1 <= count <= 128 for count in counts.values())):
            raise ProductionWorkspaceReconciliationError("composition_programme_component_invalid")
    if not component["row_counts"] or not component["relation_counts"]:
        raise ProductionWorkspaceReconciliationError("composition_programme_component_invalid")


def _programme_transition(previous, target):
    # Called by the existing stopped owner only after its actual zero-universe,
    # prior-receipt and no-pending-checkpoint checks. This codec grants nothing.
    validate_composition_progression(previous, previous)
    _validate_programme_envelope(target)
    if target["programme"] is not None or target["transition_ref"] is not None:
        raise ProductionWorkspaceReconciliationError("composition_transition_requires_empty_programmes")
    payload = {"schema_version": 1, "kind": "composition-programme-envelope-transition.v1",
        "previous_witness_digest": _programme_digest(b"seraph-continuity-prior-witness-v1\0", previous),
        "previous_schema_version": previous["schema_version"],
        "previous_extension_version": previous["extension_version"],
        "previous_extension_digest": previous["extension_digest"],
        "target_envelope_digest": _programme_digest(b"seraph-continuity-envelope-v2\0", target),
        "programme_manifest_version": PROGRAMME_MANIFEST_VERSION,
        "programme_manifest_digest": PROGRAMME_MANIFEST_DIGEST, "no_learning": True}
    return payload, _programme_digest(b"seraph-continuity-envelope-transition-v1\0", payload)


def _programme_snapshot_http_readback_digest(record, records, effects, *, job_id,
        programme_id, root, header_budget):
    """Join retained original snapshot bytes to its original HTTP address.

    The producer replaces its intent with the successful readback at that same
    effect_id. No separate persisted intent or HTTP-to-normalized-text proof
    is invented here. This digest is historical evidence, never a Source.
    """
    import re
    from src.guardian.research_plan_contracts import (
        ArtifactRef, SearchManifestV1, SourceSelectionV1, PublicSnapshotV1)
    from src.work_board.research_artifacts import read_discovery
    from src.workflows.job_runtime import _digest

    def denied():
        raise ProductionWorkspaceReconciliationError("programme_snapshot_http_association_changed")

    def original(kind):
        matches = [item for item in records if item["payload"]["kind"] == kind]
        if (len(matches) != 1 or type(matches[0]["payload"]["slot"]) is not int
                or matches[0]["payload"]["slot"] != 0):
            denied()
        return matches[0]

    def read(item, kind, limit):
        payload, artifact, adoption = item["payload"], item["artifact"], item["effect"]
        reference = ArtifactRef.model_validate(payload["artifact_ref"])
        size = payload["byte_count"]
        if (payload["kind"] != kind or payload["job_id"] != job_id
                or payload["programme_id"] != programme_id
                or type(size) is not int or type(artifact["size_bytes"]) is not int
                or artifact["size_bytes"] != size
                or artifact["artifact_id"] != reference.artifact_id
                or artifact["file_path"] != payload["file_path"]
                or artifact["content_sha256"] != reference.digest
                or adoption["content_sha256"] != reference.digest
                or type(payload["producer_fence"]) is not int or not 0 < payload["producer_fence"] < 2**63
                or type(adoption.get("fencing_token")) is not int
                or adoption["fencing_token"] != payload["producer_fence"]):
            denied()
        content = read_discovery(payload["file_path"], reference.digest, programme_id=programme_id,
            root=root, max_bytes=limit, expected_size=size, header_budget=header_budget)
        return content, reference

    payload = record["payload"]
    slot, fence = payload["slot"], payload["producer_fence"]
    if (type(slot) is not int or not 0 <= slot < 4 or type(fence) is not int
            or not 0 < fence < 2**63 or record["checkpoint_id"] != f"discovery:artifact:snapshot:{slot}"):
        denied()
    raw_snapshot, _reference = read(record, "snapshot", 65536)
    raw_manifest, manifest_ref = read(original("manifest"), "manifest", 65536)
    raw_selection, _selection_ref = read(original("selection"), "selection", 8192)
    snapshot = PublicSnapshotV1.model_validate_json(raw_snapshot)
    manifest = SearchManifestV1.model_validate_json(raw_manifest)
    selection = SourceSelectionV1.model_validate_json(raw_selection)
    selection.validate_manifest(manifest, manifest_ref)
    if slot >= len(selection.selected_result_ids) or selection.selected_result_ids[slot] != snapshot.result_id:
        denied()
    selected = next((item for item in manifest.results if item.result_id == snapshot.result_id), None)
    if selected is None or selected.exact_url != snapshot.url:
        denied()
    effect_id = f"discovery-source:{job_id}:{slot}"
    matches = [item for item in effects if item.get("effect_id") == effect_id]
    positive = [item for item in matches if item.get("receipt_kind") == "readback"
        and item.get("status") == "succeeded"]
    if not positive:
        # The artifact can precede the HTTP readback. Keep its original bytes
        # and unresolved intent/Unknown journal; absence grants no permission.
        return None
    if len(matches) != 1 or len(positive) != 1:
        denied()
    readback = positive[0]
    details = readback.get("details")
    if (readback.get("effect_type") != "public_https_read"
            or readback.get("readback_id") != "discovery-http:" + effect_id
            or readback.get("target_path") != snapshot.url
            or readback.get("target_digest") != hashlib.sha256(snapshot.url.encode("utf-8")).hexdigest()
            or type(readback.get("fencing_token")) is not int or readback["fencing_token"] != fence
            or type(readback.get("content_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", readback["content_sha256"]) is None
            or readback.get("reconciled") is not True or readback.get("reconciliation_status") != "resolved"
            or type(details) is not dict or any(details.get(key) is not True
                for key in ("verified", "read_only", "no_learning"))):
        denied()
    for field in ("recorded_at", "verified_at"):
        value = readback.get(field)
        if type(value) is not str:
            denied()
        try:
            observed = datetime.fromisoformat(value)
        except ValueError:
            denied()
        if observed.tzinfo is None or observed.utcoffset() != timezone.utc.utcoffset(observed):
            denied()
    # HTTP content hashes raw response bytes. PublicSnapshotV1 independently
    # checks normalized lines/digest, and read_discovery checked JSON file SHA.
    # Those three hashes need not match and certify no response transformation.
    return _digest(readback)


def _programme_component_on_raw_owner(owner, common33, selection, identity_certificate, *, root):
    return _programme_component_on_connection(owner._db, common33, selection,
        identity_certificate, root=root, original_owner=owner)


def _programme_component_on_connection(connection, common33, selection, identity_certificate, *, root, original_owner, _leaf_sink=None):
    """Private original rollback projection, never an execution capability."""
    from src.memory.composition_headers import _validate, snapshot_reads, read_programme_identity
    from src.work_board.research_parent import discovery_authority
    from src.workflows.research_sources import physical_discovery_closure, discovery_physical_records
    from src.workflows.job_runtime import _digest
    from src.workspace.production import _publication_json_upper
    def pay(appearance, upper):
        if common33.budget is not None:
            common33.budget.debit(upper, appearance=("programme-manifest", *appearance))
    def width(value):
        return _publication_json_upper(value)
    original_owner._validate_programme_selection(selection)
    _validate(connection, common33)
    if common33.connection is not connection:
        raise ProductionWorkspaceReconciliationError("programme_original_owner_changed")
    if not selection.runs:
        from types import MappingProxyType
        pay(("numeric-facts",), 2 + sum(width(name) + 21 for name in
            ("rows_encoded_upper", "relations_encoded_upper", "files_encoded_upper", "result_encoded_upper",
             "row_count", "relation_count", "file_count")) + 6)
        original_owner._discovery_manifest_facts = (selection, MappingProxyType({
            "rows_encoded_upper": 2, "relations_encoded_upper": 2,
            "files_encoded_upper": 2, "result_encoded_upper": 4,
            "row_count": 0, "relation_count": 0, "file_count": 0}))
        return None
    pay(("state-init",), 6 * 2)
    rows, relations, files = {}, set(), []
    for table, keys in (("goals", selection.goals), ("operator_sessions", selection.issuers)):
        for key in keys:
            with snapshot_reads(common33):
                descriptor, row, raw = _programme_reference_row_on_connection(connection, table, key)
            forecast = _SESSION_ROW_FORECAST.get()
            if (forecast is not None and forecast[0] is original_owner
                    and table == "operator_sessions" and key == forecast[1]):
                # Pure target arithmetic; the actual original row was paid/read
                # above. This value is never enrolled or issued as a certificate.
                forecast_upper = (2 + width("native-composition-memory.v1") + width(table) + width(key) + 3
                    + 2 + sum(3 + width(name) + width(forecast[2][name]) for name in descriptor.columns)
                    + max(0, len(descriptor.columns) - 1))
                pay(("issuer-forecast-copy", key), width(forecast[2]))
                for appearance in ("issuer-forecast-codec", "issuer-forecast-json", "issuer-forecast-utf8"):
                    pay((appearance, key), forecast_upper)
                raw = _native_memory_row_bytes(descriptor, key, dict(forecast[2]))
            pay(("leaf-map-entry", table, key), width(table) + width(key) + 66 + 4)
            pay(("row-hash", table, key), len(raw))
            rows[(table, key)] = hashlib.sha256(raw).hexdigest()
            if table == "goals" and row["parent_id"] is not None:
                pay(("relation", "goal_parent_fk", key), width(("goal_parent_fk", table, key, "goals", row["parent_id"])))
                relations.add(("goal_parent_fk", table, key, "goals", row["parent_id"]))
            if table == "operator_sessions":
                pay(("relation", "programme_owner_identity", key), width(("programme_owner_identity", table, key,
                    "operator_identities", row["operator_identity_id"])))
                relations.add(("programme_owner_identity", table, key,
                    "operator_identities", row["operator_identity_id"]))
    for key in selection.identity_ids:
        if identity_certificate is None or identity_certificate.connection is not connection:
            raise ProductionWorkspaceReconciliationError("programme_identity_certificate_unavailable")
        row = read_programme_identity(identity_certificate, key)
        if row is None:
            raise ProductionWorkspaceReconciliationError("programme_identity_missing")
        identity_upper = identity_certificate.rows[("operator_identities", key)][1]
        # The Identity3 raw owner paid delivery; this codec and retained leaf
        # are distinct appearances, with unchanged bytes and validation.
        identity_leaf_upper = identity_upper + width((PROGRAMME_MANIFEST_VERSION,
            PROGRAMME_MANIFEST_DIGEST, "operator_identities", key,
            ("id", "created_at", "revoked_at"))) + 16
        for appearance in ("identity-codec", "identity-json", "identity-utf8", "identity-hash"):
            pay((appearance, key), identity_leaf_upper)
        pay(("leaf-map-entry", "operator_identities", key), width("operator_identities") + width(key) + 70)
        rows[("operator_identities", key)] = _programme_identity_leaf(key, row)
    slots = set()
    for key in selection.runs:
        with snapshot_reads(common33):
            _descriptor, run, _raw = _programme_reference_row_on_connection(connection, "workflow_run_states", key)
        run_upper = common33.rows[("workflow_run_states", key)][1]
        for appearance in ("authority-json", "authority-model", "authority-binding-copy"):
            pay((appearance, key), run_upper)
        binding = discovery_authority(run["declared_authority_json"]).programme_binding
        pay(("relation", "programme_goal", key), width(("programme_goal", "workflow_run_states", key, "goals", binding.goal_id)))
        relations.add(("programme_goal", "workflow_run_states", key, "goals", binding.goal_id))
        pay(("relation", "programme_issuer_provenance", key), width(("programme_issuer_provenance", "workflow_run_states", key,
            "operator_sessions", binding.issuer_root_id)))
        relations.add(("programme_issuer_provenance", "workflow_run_states", key,
            "operator_sessions", binding.issuer_root_id))
        # Original safe bytes are mandatory before metadata enters this new
        # projection. Repeated appearances still spend the same frame's bytes.
        physical_discovery_closure(run, root=root, header_budget=common33.budget)
        pay(("effects-json", key), run_upper)
        effects = json.loads(run["effect_receipts_json"])
        for appearance in ("physical-records-parse", "physical-records-copy", "physical-records-output"):
            pay((appearance, key), run_upper)
        physical_records = discovery_physical_records(run, header_budget=common33.budget)
        for record_index, record in enumerate(physical_records):
            payload, artifact, effect = record["payload"], record["artifact"], record["effect"]
            address = (key, payload["kind"], payload["slot"])
            if address in slots:
                raise ProductionWorkspaceReconciliationError("programme_artifact_duplicate")
            slots.add(address)
            http_digest = None
            if payload["kind"] == "snapshot":
                # This historical join's original helper performs three file
                # model decodes and retained effect/digest copies. Its reads
                # remain independently charged by read_discovery below it.
                for appearance in ("snapshot-record-filter", "snapshot-reference-model",
                        "snapshot-effect-filter", "snapshot-effect-copy", "snapshot-effect-json", "snapshot-effect-utf8"):
                    pay((appearance, key, record_index), run_upper)
                for kind in ("snapshot", "manifest", "selection"):
                    original_size = sum(item["artifact"]["size_bytes"] for item in physical_records
                        if item["payload"]["kind"] == kind)
                    for appearance in ("snapshot-model-json", "snapshot-model-copy"):
                        pay((appearance, key, record_index, kind), 6 * original_size + 2)
                http_digest = _programme_snapshot_http_readback_digest(record, physical_records, effects,
                    job_id=key, programme_id=binding.programme_id, root=root, header_budget=common33.budget)
            if payload["kind"] == "manifest":
                # R53 accepted scalar rule: hash the WHOLE exact original
                # positive effect-dict vector, ordered by actual query index.
                # Any incomplete/duplicate vector projects null; original
                # effect liabilities remain intact and null grants nothing.
                from src.work_board.research_artifacts import read_discovery
                for appearance in ("query-records-parse", "query-records-copy", "query-records-output", "query-records-filter"):
                    pay((appearance, key), run_upper)
                query_records = [item for item in discovery_physical_records(run, header_budget=common33.budget)
                    if item["payload"]["kind"] == "queries"]
                if len(query_records) != 1:
                    raise ProductionWorkspaceReconciliationError("programme_queries_original_missing")
                query = query_records[0]
                content = read_discovery(query["payload"]["file_path"], query["artifact"]["content_sha256"],
                    programme_id=binding.programme_id, max_bytes=16384, root=root,
                    expected_size=query["artifact"]["size_bytes"], header_budget=common33.budget)
                pay(("queries-json", key), 6 * len(content) + 2)
                queries = json.loads(content)["queries"]
                vector = []
                for index in range(len(queries)):
                    pay(("search-effect-filter", key, index), run_upper)
                    original = [item for item in effects if item.get("effect_id") == f"discovery-search:{key}:{index}"
                        and item.get("receipt_kind") == "readback"]
                    if (len(original) != 1 or original[0].get("effect_type") != "public_https_read"
                            or original[0].get("status") != "succeeded"
                            or original[0].get("details", {}).get("search_response_receipt", {}).get("query_index") != index):
                        vector = []
                        break
                    pay(("search-effect-vector", key, index), width(original[0]))
                    vector.append(original[0])
                if vector:
                    for appearance in ("search-vector-json", "search-vector-utf8"):
                        pay((appearance, key), width(vector))
                    http_digest = _digest(vector)
            for appearance in ("effect-digest-json", "effect-digest-utf8"):
                pay((appearance, key, record_index), width(effect))
            # All selected variable file fields are retained in the paid run;
            # only the closed output keys and two digest scalars are new.
            file_upper = run_upper + width(("job_id", "programme_id", "artifact_id", "file_path", "kind", "slot",
                "content_sha256", "size_bytes", "producer_fence", "checkpoint_id", "artifact_readback_digest", "http_readback_digest")) + 132
            pay(("file-dto", key, record_index), file_upper)
            files.append({"job_id": key, "programme_id": payload["programme_id"],
                "artifact_id": artifact["artifact_id"], "file_path": payload["file_path"],
                "kind": payload["kind"], "slot": payload["slot"],
                "content_sha256": artifact["content_sha256"], "size_bytes": artifact["size_bytes"],
                "producer_fence": payload["producer_fence"], "checkpoint_id": record["checkpoint_id"],
                "artifact_readback_digest": _digest(effect), "http_readback_digest": http_digest})
    aggregate_upper = width((PROGRAMME_MANIFEST_VERSION, PROGRAMME_MANIFEST_DIGEST))
    for appearance in ("closure-prefix", "relations-prefix", "artifacts-prefix"):
        pay((appearance,), aggregate_upper)
    leaf_upper = 2 + sum(width(table) + width(key) + width(leaf) + 6
        for (table, key), leaf in rows.items()) + max(0, len(rows) - 1)
    pay(("leaf-sort",), leaf_upper)
    closure = _programme_aggregate(b"seraph-continuity-programme-closure-v1\0")
    row_counts = {}
    for (table, key), leaf in sorted(rows.items(), key=lambda item: (item[0][0], item[0][1].encode("utf-8"))):
        for field, value in (("table", table), ("key", key), ("digest", leaf)):
            pay(("leaf-prefix", table, key, field), width(value) + 8)
            _programme_length_prefix(closure, value)
        pay(("row-count-entry", table, key), width(table) + 21)
        row_counts[table] = row_counts.get(table, 0) + 1
    relation_counts = {}
    for role, *_ in relations:
        pay(("relation-count-entry", role), width(role) + 21)
        relation_counts[role] = relation_counts.get(role, 0) + 1
    relation_digest = _programme_aggregate(b"seraph-continuity-programme-relations-v1\0")
    relation_upper = width(relations)
    for appearance in ("relations-sort", "relations-json", "relations-utf8", "relations-update"):
        pay((appearance,), relation_upper)
    relation_digest.update(_programme_json(sorted(relations)))
    artifact_digest = _programme_aggregate(b"seraph-continuity-programme-artifacts-v1\0")
    file_upper = width(files)
    for appearance in ("files-sort", "files-json", "files-utf8", "files-update"):
        pay((appearance,), file_upper)
    artifact_digest.update(_programme_json(sorted(files, key=lambda item: (
        item["job_id"], item["kind"], item["slot"], item["artifact_id"]))))
    if _leaf_sink is not None:
        pay(("leaf-map-update",), leaf_upper)
        _leaf_sink.update(rows)
    result_upper = (2 + width("manifest_version") + 1 + width(PROGRAMME_MANIFEST_VERSION)
        + width("manifest_digest") + 1 + width(PROGRAMME_MANIFEST_DIGEST)
        + width("row_counts") + 1 + width(row_counts)
        + width("relation_counts") + 1 + width(relation_counts)
        + width("artifact_count") + 1 + width(len(files))
        + sum(width(field) + 1 + 66 for field in
            ("closure_digest", "relation_digest", "artifact_manifest_digest")) + 7)
    pay(("result",), result_upper)
    from types import MappingProxyType
    # Source-owned numerical observations retain no domain rows or hashes and
    # confer no certificate/selection authority on the prospective publisher.
    pay(("numeric-facts",), 2 + sum(width(name) + 21 for name in
        ("rows_encoded_upper", "relations_encoded_upper", "files_encoded_upper", "result_encoded_upper",
         "row_count", "relation_count", "file_count")) + 6)
    original_owner._discovery_manifest_facts = (selection, MappingProxyType({
        "rows_encoded_upper": leaf_upper, "relations_encoded_upper": relation_upper,
        "files_encoded_upper": file_upper, "result_encoded_upper": result_upper,
        "row_count": len(rows), "relation_count": len(relations), "file_count": len(files)}))
    return {"manifest_version": PROGRAMME_MANIFEST_VERSION, "manifest_digest": PROGRAMME_MANIFEST_DIGEST,
        "row_counts": row_counts, "closure_digest": closure.hexdigest(),
        "relation_counts": relation_counts, "relation_digest": relation_digest.hexdigest(),
        "artifact_count": len(files), "artifact_manifest_digest": artifact_digest.hexdigest()}


def original_discovery_manifest_numeric_schedule(owned, *, constructor, certificate, publication_widths):
    """Numeric projection of this original manifest's new root appearances.

    Current roots are measured by their real projection, and the aggregate
    appearances are enlarged using those numerical observations. No future
    domain row, digest, Identity certificate or execution permission is issued.
    """
    from types import MappingProxyType
    from src.workspace.production import _publication_json_upper as width
    owned.validate(db=owned.guard.db, header_budget=owned.guard.header_budget)
    guard = owned.guard
    prospective = guard._prospective_admission
    facts = getattr(guard, "_discovery_manifest_facts", None)
    if (constructor is not owned._constructor_bounds or type(constructor) is not MappingProxyType
            or prospective is None or certificate is not prospective[0].common33
            or facts is None or type(facts[1]) is not MappingProxyType
            or type(publication_widths) is not MappingProxyType
            or owned._candidate is None or owned._candidate[0] != constructor["job_id"]):
        raise ProductionWorkspaceReconciliationError("programme_numeric_manifest_unavailable")
    baseline = facts[1]
    binding = owned._candidate[1]
    key, run_upper = constructor["job_id"], constructor["row_header_bytes"]
    entries = []
    def add(appearance, upper):
        entries.append(("publication-admission", ("programme-manifest", *appearance), upper))
    add(("state-init",), 6 * 2)
    for appearance in ("authority-json", "authority-model", "authority-binding-copy", "effects-json",
            "physical-records-parse", "physical-records-copy", "physical-records-output"):
        add((appearance, key), run_upper)
    relation_extra = 0
    for role, values in (("programme_goal", ("workflow_run_states", key, "goals", binding.goal_id)),
            ("programme_issuer_provenance", ("workflow_run_states", key, "operator_sessions", binding.issuer_root_id))):
        upper = width((role, *values))
        add(("relation", role, key), upper)
        add(("relation-count-entry", role), width(role) + 21)
        relation_extra += upper + 1
    leaf_extra = 0
    for table, ids in (("goals", publication_widths["added_goals"]),
            ("operator_sessions", publication_widths["added_issuers"]),
            ("operator_identities", publication_widths["added_identities"])):
        for identity in ids:
            upper = width(table) + width(identity) + 70
            add(("leaf-map-entry", table, identity), upper)
            leaf_extra += upper + 2
            for field, value in (("table", table), ("key", identity), ("digest", None)):
                add(("leaf-prefix", table, identity, field), (66 if field == "digest" else width(value)) + 8)
            add(("row-count-entry", table, identity), width(table) + 21)
            if table == "operator_identities":
                identity_upper = publication_widths["identity_row_uppers"][identity]
                codec_upper = identity_upper + width((PROGRAMME_MANIFEST_VERSION,
                    PROGRAMME_MANIFEST_DIGEST, table, identity, ("id", "created_at", "revoked_at"))) + 16
                for appearance in ("identity-codec", "identity-json", "identity-utf8", "identity-hash"):
                    add((appearance, identity), codec_upper)
            else:
                add(("row-hash", table, identity), certificate.rows[(table, identity)][1])
                parents = publication_widths["goal_parents"] if table == "goals" else publication_widths["issuer_identities"]
                if identity not in parents:
                    raise ProductionWorkspaceReconciliationError("programme_numeric_manifest_unavailable")
                parent = parents[identity]
                if parent is not None:
                    role = "goal_parent_fk" if table == "goals" else "programme_owner_identity"
                    target = "goals" if table == "goals" else "operator_identities"
                    upper = width((role, table, identity, target, parent))
                    add(("relation", role, identity), upper)
                    add(("relation-count-entry", role), width(role) + 21)
                    relation_extra += upper + 1
    file_upper = run_upper + width(("job_id", "programme_id", "artifact_id", "file_path", "kind", "slot",
        "content_sha256", "size_bytes", "producer_fence", "checkpoint_id", "artifact_readback_digest", "http_readback_digest")) + 132
    for index in range(2):
        for appearance in ("effect-digest-json", "effect-digest-utf8"):
            add((appearance, key, index), constructor["effect_bytes"])
        add(("file-dto", key, index), file_upper)
    rows_upper = baseline["rows_encoded_upper"] + leaf_extra
    relations_upper = baseline["relations_encoded_upper"] + relation_extra
    files_upper = baseline["files_encoded_upper"] + 2 * (file_upper + 1)
    for appearance in ("closure-prefix", "relations-prefix", "artifacts-prefix"):
        add((appearance,), width((PROGRAMME_MANIFEST_VERSION, PROGRAMME_MANIFEST_DIGEST)))
    for appearance in ("leaf-sort", "leaf-map-update"):
        add((appearance,), rows_upper)
    for appearance in ("relations-sort", "relations-json", "relations-utf8", "relations-update"):
        add((appearance,), relations_upper)
    for appearance in ("files-sort", "files-json", "files-utf8", "files-update"):
        add((appearance,), files_upper)
    # Existing count keys may be absent. Bound each newly counted key and its
    # fixed int64 scalar; the result's three digests remain their original width.
    empty_result_upper = (2 + width("manifest_version") + 1 + width(PROGRAMME_MANIFEST_VERSION)
        + width("manifest_digest") + 1 + width(PROGRAMME_MANIFEST_DIGEST)
        + width("row_counts") + 3 + width("relation_counts") + 3
        + width("artifact_count") + 21 + sum(width(field) + 67 for field in
            ("closure_digest", "relation_digest", "artifact_manifest_digest")) + 7)
    result_upper = max(baseline["result_encoded_upper"], empty_result_upper) + sum(
        width(name) + 22 for name in ("goals", "operator_sessions", "operator_identities",
            "programme_goal", "programme_issuer_provenance", "goal_parent_fk", "programme_owner_identity"))
    add(("result",), result_upper)
    add(("numeric-facts",), 2 + sum(width(name) + 21 for name in
        ("rows_encoded_upper", "relations_encoded_upper", "files_encoded_upper", "result_encoded_upper",
         "row_count", "relation_count", "file_count")) + 6)
    return tuple(entries)


def original_discovery_publication_construction_numeric_schedule(owned, *, constructor, publication_widths):
    """Capacity for the original prepare-publication copies and size codecs.

    The accounting owner measures the existing facts and assigns phase
    repetitions. These scalars cannot mint a future target or certificate.
    """
    from types import MappingProxyType
    owned.validate(db=owned.guard.db, header_budget=owned.guard.header_budget)
    if (constructor is not owned._constructor_bounds or type(constructor) is not MappingProxyType
            or type(publication_widths) is not MappingProxyType
            or owned._candidate is None or constructor["job_id"] != owned._candidate[0]):
        raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
    # Each existing/changed-key set is a subset of the measured target union.
    # A delta-sort tuple also carries a before digest, bounded by its closed
    # SHA256 scalar, in addition to the measured (table,key) width.
    delta_count = max(1, len(publication_widths["added_members"]))
    amounts = (("members-difference", 2 * publication_widths["members_bytes"]),
        ("delta-key-union", 2 * publication_widths["delta_key_bytes"]),
        ("changed-touched-map", publication_widths["changed_touched_bytes"]),
        ("delta-sort", publication_widths["delta_sort_bytes"] + delta_count * (66 + 3)),
        ("delta-construction", publication_widths["delta_bytes"]),
        ("checkpoint-construction", publication_widths["checkpoint_bytes"]),
        ("receipt-default", publication_widths["receipt_default_bytes"]),
        ("receipt-target-update", publication_widths["receipt_target_update_bytes"]),
        ("receipt-witness-update", publication_widths["receipt_witness_update_bytes"]),
        ("checkpoint-size-json", publication_widths["checkpoint_bytes"]),
        ("checkpoint-size-utf8", publication_widths["checkpoint_bytes"]),
        ("receipt-size-json", publication_widths["receipt_bytes"]),
        ("receipt-size-utf8", publication_widths["receipt_bytes"]),
        ("return", publication_widths["target_bytes"] + publication_widths["checkpoint_bytes"]
            + publication_widths["receipt_bytes"] + 4))
    if any(type(upper) is not int or upper < 0 for _, upper in amounts):
        raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
    return tuple(("publication-admission", ("composition-prepare-publication", appearance), upper)
        for appearance, upper in amounts)


def composition_closure(connection, *, verify_files=None, programme_selection=None):
    """Finite native joins. Stream row bodies; publish only hashes and counts.

    ``verify_files`` is the reviewed private native reader, never child input.
    Missing extension readers block rather than inventing filesystem proof.
    """
    from src.runtime_plugins.ownership import DOMAINS, RuntimeCompositionBinding, CompositionDependency
    if programme_selection is not None:
        if type(programme_selection.owner) is CompositionSessionGuard:
            owner = programme_selection.owner
            if programme_selection.common33.connection is not connection:
                raise ProductionWorkspaceReconciliationError("programme_original_connection_unavailable")
        else:
            pair = programme_selection.owner._pair
            owner = next((item for item in (pair.source, pair.destination) if item._db is connection), None)
        if owner is None:
            raise ProductionWorkspaceReconciliationError("programme_original_connection_unavailable")
        owner._validate_programme_selection(programme_selection)
    tables = {row[0] for row in _original_composition_metadata_rows(connection,
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid LIMIT 1360", (),
        ("name",), appearance=("table-census",))}
    from src.memory.universe import native_memory_universe_on_connection
    from src.memory.composition_headers import current_budget, charge_table
    if "workflow_run_states" in tables and current_budget() is not None:
        # The whole-table certificate pays before the repeated bounded census.
        charge_table(connection, "workflow_run_states")
    memory_identities = (native_memory_universe_on_connection(connection)
                         if "workflow_run_states" in tables else ())
    # A raced-in retained Memory member must stop even the inventory body
    # read when this original operation has no complete-superset frame.
    if memory_identities and current_budget() is None:
        raise ProductionWorkspaceReconciliationError("composition_native_memory_retention_unavailable")
    if "runtime_composition_states" not in tables:
        if memory_identities:
            raise ProductionWorkspaceReconciliationError("composition_native_memory_retention_unavailable")
        return None, set()
    charge_table(connection, "runtime_composition_states")
    inventory = list(_sql(connection, "SELECT runtime_domain,owner_kind,epoch,composition_digest,state,recovery_receipt_ref FROM runtime_composition_states ORDER BY runtime_domain"))
    if not inventory:
        if memory_identities:
            raise ProductionWorkspaceReconciliationError("composition_native_memory_retention_unavailable")
        return None, set()
    if len(inventory) != 14 or {row[0] for row in inventory} != set(DOMAINS):
        raise ProductionWorkspaceReconciliationError("composition_inventory_incomplete")
    for row in inventory:
        CompositionDependency(*row[:4])
        if row[4] not in {"ready", "draining", "blocked"}:
            raise ProductionWorkspaceReconciliationError("composition_inventory_invalid")
    # The exact indexed universe includes foreign, unbound and terminal rows.
    # A current whole-superset certificate is mandatory before their bodies;
    # readable pre-effect rows confer no Source or future permission.
    for table, fields in RETAINED_FIELDS.items():
        if table not in tables:
            raise ProductionWorkspaceReconciliationError("composition_projection_schema_changed")
        validate_retained_table_schema(connection, table)
    pending = [("runtime_composition_states", row[0]) for row in inventory]
    pending.extend(("workflow_run_states", identity) for identity in memory_identities)
    charge_table(connection, "workflow_run_states")
    pending.extend(("workflow_run_states", row[0]) for row in _sql(connection,
        "SELECT run_identity FROM workflow_run_states WHERE composition_binding_json IS NOT NULL"))
    if "inference_cost_reservations" in tables:
        charge_table(connection, "inference_cost_reservations")
        pending.extend(("workflow_run_states", row[0]) for row in _sql(connection,
            "SELECT DISTINCT job_id FROM inference_cost_reservations"))
    visited = set()
    artifacts = {}
    roles = set()
    audit_predecessors = {}
    native_read_audits = {}
    native_memory_audits = {}
    def add(table, field, value):
        if value is not None:
            for row in _sql(connection, f'SELECT "{COMPOSITION_KEYS[table]}" FROM "{table}" WHERE "{field}"=?', (value,)):
                pending.append((table, _composition_address(table, row[0])))
    def required(table, value):
        if value is not None:
            pending.append((table, _composition_address(table, value)))
    def session_role(role, source_table, source_key, session_id):
        if session_id is not None:
            roles.add((role, source_table, source_key, "sessions", session_id))
            required("sessions", session_id)
    while pending:
        table, key = pending.pop()
        if (table, key) in visited:
            continue
        row = _composition_row(connection, table, key)
        visited.add((table, key))
        if table == "runtime_composition_states":
            reference = row["recovery_receipt_ref"]
            if reference is not None and reference.startswith("restore:"):
                from src.runtime_plugins.ownership import restore_audit_reference
                if row["state"] != "blocked":
                    raise ProductionWorkspaceReconciliationError("composition_restore_ready_denied")
                original = restore_audit_reference(reference)
                if original is not None:
                    from src.runtime_plugins.ownership import checked_recovery_proof
                    event = _composition_row(connection, "audit_events", original)
                    proof = checked_recovery_proof(event["event_type"], event["details_json"])
                    if proof["target"] != {field: row[field] for field in COMPOSITION_FIELDS[table][:4]}:
                        raise ProductionWorkspaceReconciliationError("composition_restore_audit_changed")
                    required("audit_events", original)
            elif reference is not None:
                from src.runtime_plugins.ownership import checked_recovery_proof
                event = _composition_row(connection, "audit_events", reference)
                proof = checked_recovery_proof(event["event_type"], event["details_json"])
                if (proof["target"] != {field: row[field] for field in COMPOSITION_FIELDS[table][:4]}
                    or proof["state"] != row["state"]):
                    raise ProductionWorkspaceReconciliationError("composition_recovery_inventory_changed")
                required("audit_events", reference)
        elif table == "audit_events":
            if key in native_memory_audits:
                expected_session, expected_digest = native_memory_audits[key]
                _, actual, encoded = _native_memory_reference_row_on_connection(connection, table, key)
                if actual["session_id"] != expected_session or hashlib.sha256(encoded).hexdigest() != expected_digest:
                    raise ProductionWorkspaceReconciliationError("composition_native_memory_audit_changed")
                session_role("audit_event_session_fk", table, key, row["session_id"])
                continue
            if key in native_read_audits:
                from src.runtime_plugins.read_journal import audit_event_body
                from src.workflows.job_runtime import _digest
                expected_session, expected_digest = native_read_audits[key]
                if row["session_id"] != expected_session or _digest(audit_event_body(row)) != expected_digest:
                    raise ProductionWorkspaceReconciliationError("composition_native_read_audit_changed")
                session_role("audit_event_session_fk", table, key, row["session_id"])
                continue
            from src.runtime_plugins.ownership import checked_recovery_proof
            proof = checked_recovery_proof(row["event_type"], row["details_json"])
            session_role("audit_event_session_fk", table, key, row["session_id"])
            reference = proof["prior_recovery_receipt_ref"]
            audit_predecessors[key] = reference
            if reference is not None and reference.startswith("restore:"):
                from src.runtime_plugins.ownership import restore_audit_reference
                original = restore_audit_reference(reference)
                audit_predecessors[key] = original
                if original is not None:
                    prior = _composition_row(connection, "audit_events", original)
                    prior_proof = checked_recovery_proof(prior["event_type"], prior["details_json"])
                    if prior_proof["target"] != proof["prior"]:
                        raise ProductionWorkspaceReconciliationError("composition_recovery_predecessor_changed")
                    required("audit_events", original)
            elif reference is not None:
                prior = _composition_row(connection, "audit_events", reference)
                prior_proof = checked_recovery_proof(prior["event_type"], prior["details_json"])
                if prior_proof["target"] != proof["prior"]:
                    raise ProductionWorkspaceReconciliationError("composition_recovery_predecessor_changed")
                required("audit_events", reference)
        if table == "workflow_run_states":
            programme_member = (programme_selection is not None and key in programme_selection.runs)
            session_role("legacy_job_session_fk", table, key, row["session_id"])
            memory_candidate = None
            if row["job_kind"] == "runtime_service_memory_v1":
                _, memory_raw_row, _ = _native_memory_reference_row_on_connection(connection, table, key)
                memory_candidate, memory_audit = _checked_memory_reference_row(connection, memory_raw_row)
                if memory_audit is not None:
                    audit_key, audit_digest = memory_audit
                    audit_proof = (row["operator_session_id"], audit_digest)
                    if audit_key in native_memory_audits and native_memory_audits[audit_key] != audit_proof:
                        raise ProductionWorkspaceReconciliationError("composition_native_memory_audit_changed")
                    native_memory_audits[audit_key] = audit_proof
                    required("audit_events", audit_key)
                source = memory_candidate.get("source")
                if source is not None:
                    required("work_board_tasks", source["task_id"])
                    required("work_board_attempts", source["attempt_id"])
            binding = row["composition_binding_json"]
            if binding is not None:
                payer = current_budget()
                if payer is not None:
                    from src.workflows.inference_accounting import (_original_native_binding_numeric_entries,
                        _accounting_prepay)
                    # Count UTF8 numerically before the original parser's
                    # size-check encoding or any model/canonical allocation.
                    text_bytes = sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in binding)
                    _accounting_prepay(payer, _original_native_binding_numeric_entries(key, text_bytes))
                native_binding = RuntimeCompositionBinding.from_json(binding)
                if row["job_kind"] == "work.local-evidence-report.v1":
                    from src.runtime_plugins.task_capability import checked_report_records
                    report = checked_report_records(SimpleNamespace(**row))
                    if (report is None or native_binding.origin_method != "tasks.admit"
                        or native_binding.native_branch != "artifact"):
                        raise ProductionWorkspaceReconciliationError("composition_report_original_profile_changed")
                    for target_table, field in (("work_board_tasks", "task_id"),
                        ("work_board_attempts", "attempt_id"), ("work_board_input_artifacts", "input_id"),
                        ("work_board_links", "link_id"), ("work_board_handoffs", "handoff_id"),
                        ("work_board_tasks", "producer_task_id"), ("work_board_attempts", "producer_attempt_id")):
                        required(target_table, report[field])
                elif row["job_kind"] == "goal_public_discovery_v1" and programme_member:
                    from src.workspace.accounting_continuity import _JOB_BINDING_FIELDS
                    original = programme_selection.runs[key]
                    if any(row[name] != original[name] for name in _JOB_BINDING_FIELDS):
                        raise ProductionWorkspaceReconciliationError("programme_original_lineage_changed")
                elif row["job_kind"] not in {"workflow", "conversation_turn_v1", "research_dossier", "readonly_research_child", "runtime_service_read_v1", "runtime_service_memory_v1"}:
                    raise ProductionWorkspaceReconciliationError("composition_extension_unsupported")
            for field in ("root_run_identity", "parent_run_identity", "parent_job_id"):
                required(table, row[field])
            dependencies = json.loads(row["dependencies_json"])
            if type(dependencies) is not list or any(type(item) is not str for item in dependencies):
                raise ProductionWorkspaceReconciliationError("composition_dependencies_invalid")
            for identity in dependencies:
                required(table, identity)
            for field in ("parent_run_identity", "parent_job_id"):
                add(table, field, key)
            for linked in ("workflow_step_states", "production_workflow_authority_states", "production_workflow_fault_receipts", "production_workflow_side_effect_receipts"):
                add(linked, "run_identity", key)
            for field in ("run_identity", "root_run_identity", "parent_run_identity"):
                add("workflow_artifact_reviews", field, key)
            add("work_board_attempts", "workflow_run_id", key)
            required("work_board_tasks", row["source_task_id"])
            if row["job_kind"] == "runtime_service_read_v1":
                from src.runtime_plugins.read_journal import read_context
                read = read_context(SimpleNamespace(**row))
                for slot in read.get("operations", []):
                    if slot["state"] == "settled" and slot["method"] == "audit.append":
                        native_read_audits[slot["audit_event_ref"]] = (row["session_id"], slot["audit_event_digest"])
                        required("audit_events", slot["audit_event_ref"])
                if row["status"] == "succeeded" and "artifact_profile" in read and (
                        len(read["operations"]) != 4 or any(slot["state"] != "settled" for slot in read["operations"])):
                    raise ProductionWorkspaceReconciliationError("composition_native_read_artifact_completion_missing")
            if row["job_kind"] == "conversation_turn_v1":
                family = checked_turn_family(row)
                if family is not None:
                    from src.agent.native_turn_family import validate_family_operation_reference
                    for operation in family["operations"]:
                        required("workflow_run_states", operation["job_id"])
                        owner_run = _composition_row(connection, "workflow_run_states", operation["job_id"])
                        if "inference_cost_reservations" not in tables:
                            raise ProductionWorkspaceReconciliationError("composition_turn_family_owner_missing")
                        charge_table(connection, "inference_cost_reservations")
                        result = _sql(connection, 'SELECT * FROM inference_cost_reservations WHERE operation_id=?', (operation["operation_id"],))
                        columns = list(result.keys()) if hasattr(result, "keys") else [item[0] for item in result.description]
                        matches = result.fetchall()
                        if len(matches) != 1 or operation["owner_id"] != row["owner_principal_id"]:
                            raise ProductionWorkspaceReconciliationError("composition_turn_family_owner_missing")
                        validate_family_operation_reference(operation, SimpleNamespace(**owner_run),
                            SimpleNamespace(**dict(zip(columns, matches[0]))))
                arguments = json.loads(row["arguments_json"])
                message_id = arguments.get("message_ref") if type(arguments) is dict else None
                if type(message_id) is not str:
                    raise ProductionWorkspaceReconciliationError("composition_turn_message_missing")
                required("messages", message_id)
                message = _composition_row(connection, "messages", message_id)
                def validate_message(candidate, role):
                    if (candidate["role"] != role or candidate["owner_principal_id"] != row["owner_principal_id"]
                        or candidate["operator_session_id"] != row["operator_session_id"]
                        or candidate["session_id"] != row["conversation_id"]
                        or candidate["conversation_id"] != row["conversation_id"]):
                        raise ProductionWorkspaceReconciliationError("composition_turn_message_binding_changed")
                validate_message(message, "user")
                if hashlib.sha256(message["content"].encode()).hexdigest() != arguments.get("content_digest"):
                    raise ProductionWorkspaceReconciliationError("composition_turn_input_digest_changed")
                for checkpoint in json.loads(row["checkpoint_receipts_json"]):
                    if checkpoint.get("checkpoint_id") == "conversation:controlled-outcome":
                        from src.workflows.job_runtime import _digest
                        from uuid import uuid5, NAMESPACE_URL
                        payload = checkpoint.get("payload")
                        if (type(payload) is not dict or payload.get("schema_version") != 1
                            or type(payload.get("schema_version")) is not int or payload.get("input_message_ref") != message_id
                            or payload.get("no_learning") is not True or checkpoint.get("safe") is not True
                            or checkpoint.get("state_digest") != _digest(payload)):
                            raise ProductionWorkspaceReconciliationError("composition_controlled_receipt_changed")
                        common = {"schema_version", "input_message_ref", "no_learning", "outcome"}
                        if payload.get("outcome") == "clarification_required" and set(payload) == common | {"message_ref"}:
                            output = _composition_row(connection, "messages", payload["message_ref"])
                            validate_message(output, "assistant")
                            if output["id"] != uuid5(NAMESPACE_URL,
                                f"seraph-chat:{row['owner_principal_id']}:{row['conversation_id']}:{message_id}:clarification").hex:
                                raise ProductionWorkspaceReconciliationError("composition_clarification_binding_changed")
                            required("messages", output["id"])
                        elif payload.get("outcome") == "approval_required" and set(payload) == common | {"approval_ref"}:
                            approval = _composition_row(connection, "approval_requests", payload["approval_ref"])
                            if (approval["owner_principal_id"] != row["owner_principal_id"]
                                or approval["operator_session_id"] != row["operator_session_id"]
                                or approval["conversation_id"] != row["conversation_id"]
                                or not approval["tool_name"] or not approval["fingerprint"]):
                                raise ProductionWorkspaceReconciliationError("composition_approval_binding_changed")
                            required("approval_requests", approval["id"])
                        else:
                            raise ProductionWorkspaceReconciliationError("composition_controlled_receipt_changed")
                    if checkpoint.get("checkpoint_id") == "conversation:assistant-message":
                        from src.workflows.job_runtime import _digest
                        payload = checkpoint.get("payload")
                        if (type(payload) is not dict or set(payload) != {"schema_version", "message_ref", "input_message_ref", "no_learning"}
                            or payload["schema_version"] != 1 or payload["input_message_ref"] != message_id
                            or payload["no_learning"] is not True or checkpoint.get("safe") is not True
                            or checkpoint.get("state_digest") != _digest(payload)):
                            raise ProductionWorkspaceReconciliationError("composition_turn_output_receipt_changed")
                        output = _composition_row(connection, "messages", payload["message_ref"])
                        validate_message(output, "assistant")
                        required("messages", output["id"])
            from src.runtime_plugins.inference_output import checked_output_records, checked_candidate_record
            marker = checked_candidate_record(row)
            if marker is not None:
                from src.model_fabric.native_inference import validate_candidate_binding
                marker_payload = marker["payload"]
                required("workflow_run_states", marker_payload["turn_job_id"])
                marker_turn = _composition_row(connection, "workflow_run_states", marker_payload["turn_job_id"])
                marker_family = checked_turn_family(marker_turn)
                if marker_family is None or marker_family["original_claim_digest"] != marker_payload["turn_claim_digest"]:
                    raise ProductionWorkspaceReconciliationError("composition_inference_candidate_claim_changed")
                charge_table(connection, "inference_cost_reservations")
                result = _sql(connection, 'SELECT * FROM inference_cost_reservations WHERE operation_id=?', (marker_payload["operation_id"],))
                columns = list(result.keys()) if hasattr(result, "keys") else [item[0] for item in result.description]
                matches = result.fetchall()
                if len(matches) != 1:
                    raise ProductionWorkspaceReconciliationError("composition_inference_candidate_owner_missing")
                validate_candidate_binding(marker_payload, SimpleNamespace(**row), SimpleNamespace(**dict(zip(columns, matches[0]))))
            outputs = checked_output_records(row)
            if outputs is not None:
                if marker is None:
                    raise ProductionWorkspaceReconciliationError("composition_inference_output_candidate_missing")
                payload = outputs[0]["payload"]
                required("workflow_run_states", payload["turn_job_id"])
                turn_row = _composition_row(connection, "workflow_run_states", payload["turn_job_id"])
                family = checked_turn_family(turn_row)
                if (family is None or payload["family_call_index"] >= len(family["operations"])
                    or payload["turn_claim_digest"] != family["original_claim_digest"]
                    or turn_row["owner_principal_id"] != payload["owner_id"]):
                    raise ProductionWorkspaceReconciliationError("composition_inference_output_family_changed")
                operation = family["operations"][payload["family_call_index"]]
                for field, other in (("operation_id", "operation_id"), ("accounting_job_id", "job_id"),
                        ("reservation_binding_digest", "reservation_binding_digest"), ("fencing_token", "fencing_token"),
                        ("attempt_count", "attempt_count"), ("reservation_sequence", "reservation_sequence"),
                        ("payload_digest", "payload_digest"), ("policy_digest", "policy_digest"),
                        ("profile_id", "profile_id"), ("runtime_path", "runtime_path")):
                    if payload[field] != operation[other]:
                        raise ProductionWorkspaceReconciliationError("composition_inference_output_operation_changed")
            if (binding is not None or outputs is not None) and (json.loads(row["artifact_receipts_json"]) or json.loads(row["checkpoint_receipts_json"])) and not programme_member:
                if verify_files is None:
                    raise ProductionWorkspaceReconciliationError("composition_private_reader_unavailable")
                for ref, digest, size, classification in verify_files(table, row):
                    value = (ref, digest, size, classification)
                    if ref in artifacts and artifacts[ref] != value:
                        raise ProductionWorkspaceReconciliationError("composition_artifact_conflict")
                    artifacts[ref] = value
        elif table == "approval_requests":
            session_role("approval_request_session_fk", table, key, row["session_id"])
        elif table == "work_board_tasks":
            required("work_board_input_artifacts", row["input_artifact_id"])
            for linked in ("work_board_attempts", "work_board_review_intents", "work_board_events", "work_board_evidence_dependencies"):
                add(linked, "task_id", key)
            for field in ("parent_task_id", "child_task_id"):
                add("work_board_links", field, key)
                add("work_board_handoffs", field, key)
        elif table == "work_board_attempts":
            required("work_board_tasks", row["task_id"])
            required("workflow_run_states", row["workflow_run_id"])
        elif table in {"work_board_links", "work_board_handoffs"}:
            for field in ("parent_task_id", "child_task_id"):
                required("work_board_tasks", row[field])
            if table == "work_board_handoffs":
                required("work_board_links", row["link_id"])
                required("work_board_attempts", row["source_attempt_id"])
                required("workflow_run_states", row["workflow_run_id"])
        elif table == "messages":
            if row["attachment_refs_json"] not in {None, "[]"}:
                raise ProductionWorkspaceReconciliationError("composition_attachment_extension_unsupported")
            session = _composition_row(connection, "sessions", row["session_id"])
            if session["owner_principal_id"] != row["owner_principal_id"]:
                raise ProductionWorkspaceReconciliationError("composition_turn_session_owner_changed")
            session_role("conversation_context", table, key, row["session_id"])
            add("memory_episodes", "source_message_id", key)
        elif table == "memory_episodes":
            message = _composition_row(connection, "messages", row["source_message_id"])
            if (row["session_id"] != message["session_id"] or row["subject_entity_id"] is not None
                    or row["project_entity_id"] is not None):
                raise ProductionWorkspaceReconciliationError("composition_turn_episode_extension_unsupported")
        elif table == "work_board_input_artifacts":
            if verify_files is None:
                raise ProductionWorkspaceReconciliationError("composition_private_reader_unavailable")
            for ref, digest, size, classification in verify_files(table, row):
                artifacts[ref] = (ref, digest, size, classification)
    for identifier in audit_predecessors:
        trail = set()
        current = identifier
        while current is not None:
            if current in trail:
                raise ProductionWorkspaceReconciliationError("composition_recovery_predecessor_cycle")
            trail.add(current)
            current = audit_predecessors.get(current)
    payer = current_budget()
    from src.workspace.production import _publication_json_upper as width
    def pay(stage, upper, *address):
        if payer is not None:
            payer.debit(upper, appearance=("native-closure-tail", stage, *address))
    visited_width = 2 + sum(width(item) for item in visited) + max(0, len(visited) - 1)
    pay("closure-prefix", len(b"seraph-continuity-closure-v1\0"))
    pay("visited-sort", visited_width)
    for table, key in visited:
        pay("key-utf8", sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4
            for c in key), table, key)
    pay("table-counts", 2 + sum(width(table) + 22 for table, key in visited))
    digest = hashlib.sha256(b"seraph-continuity-closure-v1\0")
    counts = {}
    for table, key in sorted(visited, key=lambda item: (item[0], item[1].encode())):
        leaf = composition_row_digest(table, key, _composition_row(connection, table, key))
        for field, value in (("table", table), ("key", key), ("leaf", leaf)):
            value_width = sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in value)
            for stage, upper in (("utf8", value_width), ("length-prefix", 8), ("hash-prefix", 8), ("hash-value", value_width)):
                pay(stage, upper, table, key, field)
            encoded = value.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        counts[table] = counts.get(table, 0) + 1
    inventory_width = 2 + max(0, len(inventory) - 1)
    for row in inventory:
        item_width = 2 + sum(width(field) + 1 + width(value)
            for field, value in zip(COMPOSITION_FIELDS["runtime_composition_states"], row)) + len(row) - 1
        pay("inventory-row-copy", item_width)
        inventory_width += item_width
    pay("inventory-list", inventory_width)
    for stage in ("inventory-json", "inventory-utf8", "inventory-hash"):
        pay(stage, inventory_width)
    pay("inventory-digest-return", 66)
    inventory_payload = [dict(zip(COMPOSITION_FIELDS["runtime_composition_states"], row)) for row in inventory]
    pay("role-counts", 2 + sum(width(role) + 22 for role, *_ in roles))
    role_counts = {}
    for role, *_ in roles:
        role_counts[role] = role_counts.get(role, 0) + 1
    relation_width = 2 + sum(width(role) for role in roles) + max(0, len(roles) - 1)
    artifact_width = 2 + sum(width(item) for item in artifacts.values()) + max(0, len(artifacts) - 1)
    for stage in ("relations-sort", "relations-json", "relations-utf8", "relations-hash"):
        pay(stage, relation_width)
    for stage in ("artifacts-sort", "artifacts-json", "artifacts-utf8", "artifacts-hash"):
        pay(stage, artifact_width)
    for stage in ("closure-digest-return", "relations-digest-return", "artifacts-digest-return"):
        pay(stage, 66)
    result_widths = {"schema_version": 1, "projection_schema": width(COMPOSITION_SCHEMA),
        "extension_version": width(NATIVE_EXTENSION_VERSION), "extension_digest": 66,
        "inventory": inventory_width, "inventory_digest": 66, "closure_digest": 66,
        "total_rows": width(len(visited)), "table_counts": width(counts), "relation_counts": width(role_counts),
        "relation_digest": 66, "artifact_count": width(len(artifacts)), "artifact_manifest_digest": 66}
    pay("result", 2 + sum(width(field) + 1 + upper for field, upper in result_widths.items()) + len(result_widths) - 1)
    pay("visited-return", visited_width)
    return {"schema_version": 1, "projection_schema": COMPOSITION_SCHEMA,
        "extension_version": NATIVE_EXTENSION_VERSION, "extension_digest": NATIVE_EXTENSION_DIGEST,
        "inventory": inventory_payload,
        "inventory_digest": hashlib.sha256(json.dumps(inventory_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "closure_digest": digest.hexdigest(), "total_rows": len(visited), "table_counts": counts,
        "relation_counts": role_counts, "relation_digest": hashlib.sha256(json.dumps(sorted(roles), separators=(",", ":")).encode()).hexdigest(),
        "artifact_count": len(artifacts), "artifact_manifest_digest": hashlib.sha256(json.dumps(
            sorted(artifacts.values()), separators=(",", ":")).encode()).hexdigest()}, visited


def validate_composition_progression(previous, incoming, *, programme_transition=None):
    previous_v2 = type(previous) is dict and previous.get("schema_version") == 2
    incoming_v2 = type(incoming) is dict and incoming.get("schema_version") == 2
    if previous_v2 or incoming_v2:
        if not incoming_v2:
            raise ProductionWorkspaceReconciliationError("composition_envelope_downgrade_forbidden")
        _validate_programme_envelope(incoming)
        if previous_v2:
            _validate_programme_envelope(previous)
            if incoming["transition_ref"] != previous["transition_ref"]:
                raise ProductionWorkspaceReconciliationError("composition_transition_reference_changed")
            validate_composition_progression(previous["native"], incoming["native"])
            return
        # An ordinary publication never silently upgrades a v1 witness. The
        # explicit stopped transition is checked against its exact prior bytes.
        target = dict(incoming, transition_ref=None)
        payload, reference = _programme_transition(previous, target)
        if programme_transition != payload or incoming["transition_ref"] != reference:
            raise ProductionWorkspaceReconciliationError("composition_explicit_transition_required")
        validate_composition_progression(previous, incoming["native"])
        return
    from src.runtime_plugins.ownership import DOMAINS, CompositionDependency
    required = {"schema_version", "projection_schema", "inventory", "inventory_digest", "closure_digest",
                "total_rows", "table_counts", "artifact_count", "artifact_manifest_digest", "extension_version", "extension_digest",
                "relation_counts", "relation_digest"}
    def checked(value):
        if type(value) is not dict or set(value) != required or value["schema_version"] != 1 or value["projection_schema"] != COMPOSITION_SCHEMA:
            raise ProductionWorkspaceReconciliationError("composition_witness_invalid")
        if value["extension_version"] != NATIVE_EXTENSION_VERSION or value["extension_digest"] != NATIVE_EXTENSION_DIGEST:
            raise ProductionWorkspaceReconciliationError("composition_extension_manifest_changed")
        rows = value["inventory"]
        if type(rows) is not list or len(rows) != 14 or [row.get("runtime_domain") for row in rows] != sorted(DOMAINS):
            raise ProductionWorkspaceReconciliationError("composition_inventory_incomplete")
        for row in rows:
            if set(row) != set(COMPOSITION_FIELDS["runtime_composition_states"]):
                raise ProductionWorkspaceReconciliationError("composition_inventory_invalid")
            CompositionDependency(*(row[field] for field in COMPOSITION_FIELDS["runtime_composition_states"][:4]))
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != value["inventory_digest"]:
            raise ProductionWorkspaceReconciliationError("composition_inventory_digest_changed")
        return rows
    older, newer = checked(previous), checked(incoming)
    for before, after in zip(older, newer):
        if (after["epoch"] < before["epoch"] or (after["epoch"] == before["epoch"]
                and (after["owner_kind"], after["composition_digest"]) != (before["owner_kind"], before["composition_digest"]))):
            raise ProductionWorkspaceReconciliationError("composition_high_water_conflict")


def native_composition_files(table, row, *, root=None, header_budget=None):
    """Positive bytes through the existing fixed private artifact reader."""
    from types import SimpleNamespace
    from config.settings import settings
    from src.memory.composition_headers import current_budget
    header_budget = header_budget if header_budget is not None else current_budget()
    from src.workspace import canonical_workspace_root
    from src.work_board.input_artifacts import _payload_path, _safe_file_bytes
    if table == "work_board_input_artifacts":
        path = _payload_path(SimpleNamespace(**row))
        if root is not None:
            path = Path(root) / row["typed_input_ref"][len("workspace-json:"):]
        _safe_file_bytes(path, expected_digest=row["payload_sha256"], expected_size=row["size_bytes"], header_budget=header_budget)
        yield row["typed_input_ref"], row["payload_sha256"], row["size_bytes"], "private_input"
        return
    if table != "workflow_run_states":
        raise ProductionWorkspaceReconciliationError("composition_private_reader_unsupported")
    from src.runtime_plugins.inference_output import checked_output_records, OUTPUT_ID, read_output_bytes, verify_output_envelope
    outputs = checked_output_records(row)
    if outputs is not None and outputs[-1]["checkpoint_id"] == OUTPUT_ID:
        payload = outputs[-1]["payload"]
        workspace = Path(root) if root is not None else canonical_workspace_root(settings.workspace_dir)
        verify_output_envelope(read_output_bytes(workspace, payload, header_budget=header_budget), payload)
        yield payload["file_ref"], payload["content_sha256"], payload["size_bytes"], "private_inference_output"
    refs = json.loads(row["artifact_receipts_json"])
    for checkpoint in json.loads(row["checkpoint_receipts_json"]):
        if type(checkpoint) is not dict:
            raise ProductionWorkspaceReconciliationError("composition_checkpoint_invalid")
        identifier = checkpoint.get("checkpoint_id")
        if type(identifier) is str and identifier.startswith("research:artifact:"):
            payload = checkpoint.get("payload")
            if type(payload) is not dict or payload.get("job_id") != row["run_identity"]:
                raise ProductionWorkspaceReconciliationError("composition_artifact_binding_changed")
            refs.append({"file_path": payload.get("file_path"), "content_sha256": payload.get("content_sha256"),
                         "size_bytes": payload.get("byte_count")})
    for receipt in refs:
        if type(receipt) is not dict:
            raise ProductionWorkspaceReconciliationError("composition_artifact_receipt_invalid")
        reference, digest, size = receipt.get("file_path"), receipt.get("content_sha256"), receipt.get("size_bytes")
        if (type(reference) is not str or not reference.startswith("artifacts/work-board/")
            or ".." in Path(reference).parts or Path(reference).is_absolute()
            or type(digest) is not str or len(digest) != 64 or type(size) is not int or not 0 <= size <= 1024 * 1024):
            raise ProductionWorkspaceReconciliationError("composition_artifact_extension_unsupported")
        _safe_file_bytes((Path(root) if root is not None else canonical_workspace_root(settings.workspace_dir)) / reference,
                         expected_digest=digest, expected_size=size, header_budget=header_budget)
        yield reference, digest, size, "private_artifact"


class CompositionReadGuard:
    """Generic inspection remains usable; no retained mutation permission."""
    def __init__(self, db):
        self.db = db
        self.listeners = []
        self.connection_listeners = []
        self.closed = False

    def install(self):
        from sqlalchemy import event
        from sqlalchemy.sql.elements import TextClause
        def denied():
            error = ProductionWorkspaceReconciliationError("composition_native_writer_required")
            self.db.info["composition_sticky_failure"] = error
            raise error
        def before_flush(session, context, instances):
            if any(getattr(value, "__tablename__", None) in RETAINED_FIELDS
                   for value in (*session.new, *session.dirty, *session.deleted)):
                denied()
        def before_execute(state):
            operation = state.statement
            table = getattr(getattr(operation, "table", None), "name", None)
            if (state.is_insert or state.is_update or state.is_delete) and table in RETAINED_FIELDS:
                denied()
            if isinstance(operation, TextClause):
                source = operation.text.lower().lstrip()
                if not source.startswith(("select", "begin", "pragma", "savepoint", "release", "rollback")) and any(name in source for name in RETAINED_FIELDS):
                    denied()
        def after_begin(session, transaction, connection):
            def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                compiled = getattr(context, "compiled", None)
                operation = getattr(compiled, "statement", None)
                table = getattr(getattr(operation, "table", None), "name", None)
                if compiled is None or isinstance(operation, TextClause):
                    source = statement.lower().lstrip()
                    protected = not source.startswith(("select", "begin", "pragma", "savepoint", "release", "rollback")) and any(name in source for name in RETAINED_FIELDS)
                else:
                    protected = table in RETAINED_FIELDS and any(getattr(operation, flag, False) for flag in ("is_insert", "is_update", "is_delete"))
                if protected:
                    denied()
            event.listen(connection, "before_cursor_execute", before_cursor_execute)
            self.connection_listeners.append((connection, before_cursor_execute))
        def before_commit(session):
            if session.info.get("composition_sticky_failure") is not None:
                raise session.info["composition_sticky_failure"]
        for name, callback in (("before_flush", before_flush), ("do_orm_execute", before_execute),
                               ("after_begin", after_begin), ("before_commit", before_commit)):
            event.listen(self.db.sync_session, name, callback)
            self.listeners.append((name, callback))

    def close(self):
        from sqlalchemy import event
        if self.closed:
            return
        self.closed = True
        for name, callback in self.listeners:
            event.remove(self.db.sync_session, name, callback)
        for connection, callback in self.connection_listeners:
            event.remove(connection, "before_cursor_execute", callback)


async def prepare_composition_read_session(db, *, header_budget=None):
    """Cheap inventory-presence check; never private files or publication."""
    from sqlalchemy import text
    exists = await db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_composition_states'"))
    populated = await db.scalar(text("SELECT 1 FROM runtime_composition_states LIMIT 1")) if exists else None
    if header_budget is not None:
        from src.memory.composition_headers import _metadata_cost
        header_budget.debit(_metadata_cost([exists, populated]), appearance=("composition-inventory-probes",))
    await db.rollback()
    if not populated:
        return None
    guard = CompositionReadGuard(db)
    guard.install()
    db.info["composition_read_guard"] = guard
    return guard


_SESSION_ROW_FORECAST = ContextVar("session_row_forecast", default=None)
# Ephemeral original issuer provenance; never a Source or writer permission.
_WRITER_FIRST_METADATA_GUARDS = WeakSet()
# A shallow copy never ran the original guard constructor.
_COMPOSITION_METADATA_GUARDS = WeakSet()

class CompositionSessionGuard:
    """One existing session, external lock and precommit publisher.

    Native owners opt into their complete atomic writer. Unmarked writes to
    retained leaves (including bulk SQL) fail before mutation/publication.
    """
    def __init__(self, db, workspace, lock, witness_value, members, header_budget=None):
        self.db, self.workspace, self.lock = db, workspace, lock
        self.base, self.members = witness_value, members
        self.header_budget = header_budget
        self._pending_events = []
        self.touched = {}
        self.token = _HELD_COMPOSITION_WORKSPACE.set(workspace)
        self.listeners = []
        self.connection_listeners = []
        self.prepared_commit = False
        self._legacy_continuity_metadata = {}
        self._retention_closed = False
        self._retention_writer_snapshot = None
        self._retention_read_budget = None
        self._writer_first_metadata_mode = None
        self._writer_first_metadata_frame = None
        self._selection = None
        self._prospective_admission = None
        self._prospective_admission_facts = None
        self._prospective_admission_finished = False
        self._discovery_publication_exit = None
        self._discovery_commit_exit = None
        self._discovery_publication_snapshot = None
        self._discovery_writer_metadata_owner = None
        self._discovery_reservation = None
        self._publication_members = None
        self._programme_snapshot_owner = None
        self._programme_current_leaves = {}
        self._programme_base_leaves = {}
        self._programme_writer = None
        self._session_reservation = None
        self._session_expected = None
        self._session_instance = None
        _COMPOSITION_METADATA_GUARDS.add(self)

    def _programme_tables(self):
        return {"goals", "operator_sessions", "operator_identities"} if (
            type(self.base) is dict and self.base.get("schema_version") == 2) else set()

    def _protected_tables(self):
        return set(RETAINED_FIELDS) | self._programme_tables()

    def _address(self, table, key):
        if table in self._programme_tables():
            if type(key) is not str or not 0 < len(key.encode("utf-8")) <= 512:
                raise ProductionWorkspaceReconciliationError("programme_row_address_invalid")
            return key
        return _composition_address(table, key)

    def _ensure_programme_writer(self, connection):
        if not self._programme_tables():
            return
        self._validate_native_writer_snapshot(self.header_budget, connection)
        current = (connection, connection.get_transaction(), connection.connection.driver_connection)
        if self._programme_writer is not None:
            if any(actual is not original for actual, original in zip(current, self._programme_writer)):
                raise ProductionWorkspaceReconciliationError("programme_current_writer_changed")
            return
        from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection
        certificate = _certify_current_memory_snapshot_on_connection(connection, self.header_budget,
            _header_budget=self._writer_metadata_budget(connection))
        observed, members = self._programme_snapshot(connection, certificate, self.base)
        if observed != self.base or members != self.members:
            raise ProductionWorkspaceReconciliationError("programme_fresh_writer_conflict")
        self._programme_writer = current

    def _leaf(self, connection, table, key, *, original=False):
        if table in self._programme_tables():
            leaves = self._programme_base_leaves if original else self._programme_current_leaves
            leaf = leaves.get((table, key))
            if leaf is None:
                raise ProductionWorkspaceReconciliationError("programme_selected_leaf_unavailable")
            return leaf
        owned = self._discovery_writer_metadata_owner
        if owned is not None:
            from src.memory.composition_headers import _CURRENT, snapshot_reads
            from src.db.engine import _original_discovery_exit_completion
            owned.validate(db=self.db, header_budget=self.header_budget)
            if _CURRENT.get() is None:
                certificate = (self._publication_common if _original_discovery_exit_completion.get() is not None
                    else self._discovery_latest_common)
                with snapshot_reads(certificate):
                    return composition_row_digest(table, key, _composition_row(connection, table, key))
        return composition_row_digest(table, key, _composition_row(connection, table, key))

    def _validate_programme_selection(self, selection):
        import asyncio
        import threading
        from src.workspace.accounting_continuity import _ProgrammeSelection, _PROGRAMME_SELECTION_SEAL
        from src.memory.composition_headers import _validate
        prospective = self._prospective_admission
        is_prospective = prospective is not None and prospective[0] is selection
        if (type(selection) is not _ProgrammeSelection or selection.seal is not _PROGRAMME_SELECTION_SEAL
                or selection.issued_id != id(selection) or selection.owner is not self
                or (self._selection is not selection and not is_prospective) or self._retention_closed
                or self.db.info.get("composition_guard") is not self
                or (prospective[1] if is_prospective else self._programme_snapshot_owner)
                    != (asyncio.current_task(), threading.get_ident())
                or self.db.sync_session.connection() is not selection.common33.connection
                or selection.common33.budget is not self.header_budget):
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        _validate(selection.common33.connection, selection.common33)
        if is_prospective:
            service, host, operation = prospective[2:5]
            if self._original_programme_admission_owner(service, host) is not operation:
                raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
            reviewed, boot, process, dispatcher, jobs = prospective[5]
            if (host.reviewed is not reviewed or host.boot_nonce != boot or host.process is not process
                    or host.service_dispatch is not dispatcher or service.jobs is not jobs):
                raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")

    def _original_programme_admission_owner(self, service, host):
        """Only the actual lifecycle/admission owner can bind capacity selection."""
        import asyncio
        from src.guardian.goal_discovery import GoalDiscoveryService, goal_discovery_service
        from src.runtime_plugins.bridge import CordisHost, cordis_host
        if (type(service) is not GoalDiscoveryService or service is not goal_discovery_service
                or type(host) is not CordisHost or host is not cordis_host):
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        validator = getattr(GoalDiscoveryService, "_validate_original_admission_owner", None)
        if validator is None:
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        operation = validator(service, jobs=service.jobs, host=host, task=asyncio.current_task())
        if operation is None or operation is not asyncio.current_task():
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        return operation

    def _select_programme_admission(self, connection, common33, *, service, host,
                                   goal_id, programme_id, grant_revision):
        """Private ADR-034 capacity selection, never admission or publication."""
        import asyncio
        import threading
        from types import MappingProxyType
        from src.workspace.accounting_continuity import _ProgrammeSelection, _derive_programme_admission_rows
        from src.memory.composition_headers import _validate, preflight_programme_identity_component
        def fail(code):
            raise ProductionWorkspaceReconciliationError(code)
        if (not self._programme_tables() or self.db.info.get("composition_guard") is not self
                or self._retention_closed or self._prospective_admission_finished or common33.raw_owner is not None
                or common33.connection is not connection or common33.budget is not self.header_budget
                or self.db.sync_session.connection() is not connection):
            fail("programme_original_selection_unavailable")
        _validate(connection, common33)
        operation = self._original_programme_admission_owner(service, host)
        owner = (host.reviewed, host.boot_nonce, host.process, host.service_dispatch, service.jobs)
        # A failed rederivation never leaves a previous prospective certificate usable.
        self._prospective_admission = None
        programme, binding, goals, issuers = _derive_programme_admission_rows(connection, common33,
            goal_id=goal_id, programme_id=programme_id, grant_revision=grant_revision, fail=fail)
        facts = (operation, owner, binding, goals, issuers)
        previous = self._prospective_admission_facts
        if previous is not None:
            old = previous[1]
            if (previous[0] is not operation or old[0] is not owner[0] or old[1] != owner[1]
                    or any(old[index] is not owner[index] for index in (2, 3, 4))):
                fail("programme_original_selection_unavailable")
            if previous[2:] != facts[2:]:
                fail("programme_original_generation_changed")
        selection = _ProgrammeSelection(self, common33, MappingProxyType(goals), MappingProxyType(issuers),
            MappingProxyType({}), (binding.owner_identity_id,))
        object.__setattr__(selection, "issued_id", id(selection))
        self._prospective_admission = (selection, (asyncio.current_task(), threading.get_ident()),
            service, host, operation, owner)
        try:
            self._validate_programme_selection(selection)
            identity = preflight_programme_identity_component(common33, original_selection=selection)
        except BaseException:
            self._prospective_admission = None
            raise
        self._prospective_admission_facts = facts
        return programme, binding, common33, identity

    def _check_original_discovery_incoming(self, connection, certificate, *, service, host, job_id):
        """Original certified first-constructor orphan check; capacity only.

        These are exactly the ten reverse joins followed by composition_closure
        for a WRS node. An existing edge cannot be adopted by minting that absent
        root. SELECT1 returns bounded existence metadata, never foreign bodies.
        The actual initial reader and fresh writer each pay their own check.
        """
        from src.memory.composition_headers import _validate
        prospective = self._prospective_admission
        if (prospective is None or prospective[0].common33 is not certificate
                or prospective[2] is not service or prospective[3] is not host
                or certificate.connection is not connection
                or certificate.budget is not self.header_budget
                or type(job_id) is not str or not job_id.startswith("goal-discovery:")
                or len(job_id) != len("goal-discovery:") + 32
                or any(char not in "0123456789abcdef" for char in job_id[len("goal-discovery:"):])):
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        self._validate_programme_selection(prospective[0])
        if self._original_programme_admission_owner(service, host) is not prospective[4]:
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        # Bind the query locator to the original service's current occurrence,
        # not merely to a syntactically valid absent discovery ID.
        from datetime import timezone
        from uuid import uuid5, NAMESPACE_URL
        from src.guardian.goal_programmes import goal_programme_service
        binding = self._prospective_admission_facts[2]
        day = goal_programme_service._clock().astimezone(timezone.utc).date().isoformat()
        identifier = uuid5(NAMESPACE_URL,
            f"seraph:public-discovery:{binding.owner_identity_id}:{binding.programme_id}:{day}")
        if job_id != "goal-discovery:" + identifier.hex:
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        _validate(connection, certificate)
        joins = (("workflow_run_states", "parent_run_identity"), ("workflow_run_states", "parent_job_id"),
            *((table, "run_identity") for table in ("workflow_step_states", "production_workflow_authority_states",
                "production_workflow_fault_receipts", "production_workflow_side_effect_receipts")),
            *(("workflow_artifact_reviews", field) for field in ("run_identity", "root_run_identity", "parent_run_identity")),
            ("work_board_attempts", "workflow_run_id"))
        for table, field in joins:
            # Maximum metadata [[1]] is four bytes; absence [] is two. The
            # difference stays spent and is never a capacity refund.
            self.header_budget.debit(4, appearance=("discovery-incoming", table, field, job_id))
            result = _sql(connection, f'SELECT 1 FROM "{table}" WHERE "{field}"=? LIMIT 1', (job_id,)).fetchone()
            if result is not None:
                raise ProductionWorkspaceReconciliationError("programme_original_incoming_provenance_exists")
        _validate(connection, certificate)

    async def _original_discovery_persisted_authority_charges(self, discovery_session,
            *, constructor, trace, certificate):
        """Project the original persisted selector, never issue future authority.

        Existing roots and both conditional Identity selections are measured by
        their real owners. Only the absent constructor's numeric row width and
        the already selected Goal/issuer provenance are added to that schedule.
        """
        from src.memory.header_bounds import (_trace_memory_numeric_charges,
            _discovery_projected_superset_charges)
        from src.memory.composition_headers import (_validate,
            preflight_programme_identity_component, read_programme_identity)
        import inspect
        from src.guardian import goal_programmes
        source_owner = getattr(goal_programmes, "original_discovery_authority_numeric_schedule", None)
        if (source_owner is None or inspect.currentframe().f_back.f_code is not source_owner.__code__):
            raise ProductionWorkspaceReconciliationError("programme_numeric_trace_changed")
        discovery_session.validate(db=self.db, header_budget=self.header_budget)
        prospective = self._prospective_admission
        if (discovery_session.guard is not self or prospective is None
                or constructor is not discovery_session._constructor_bounds
                or certificate is not prospective[0].common33
                or discovery_session._candidate is None
                or discovery_session._candidate[0] != constructor["job_id"]
                or discovery_session._candidate[1] != self._prospective_admission_facts[2]
                or self.db.new or self.db.dirty or self.db.deleted):
            raise ProductionWorkspaceReconciliationError("programme_numeric_trace_changed")
        binding = self._prospective_admission_facts[2]
        connection = await self.db.connection()
        def measured(conn):
            _validate(conn, certificate)
            self._validate_programme_selection(prospective[0])
            with _trace_memory_numeric_charges(self.header_budget) as selected_trace:
                selected = self._select_programmes(conn, certificate)
                identity_ids = tuple(sorted(set(selected.identity_ids) | {binding.owner_identity_id}))
                identity_uppers = {}
                # Additive measurements conservatively include the shared
                # compiler/state overhead twice; they never waive a repeat.
                for original in (selected, prospective[0]):
                    if not original.identity_ids:
                        continue
                    identity = preflight_programme_identity_component(certificate,
                        original_selection=original)
                    identity_uppers.update((key, upper) for (table, key), (_, upper) in identity.rows.items())
                    for key in original.identity_ids:
                        read_programme_identity(identity, key)
            return tuple(selected_trace), identity_ids, identity_uppers
        selected_trace, identity_ids, identity_uppers = await connection.run_sync(measured)
        from types import MappingProxyType
        from src.workspace.production import _publication_json_upper
        self.header_budget.debit(_publication_json_upper(identity_uppers),
            appearance=("discovery-identity-width-output", constructor["job_id"]))
        self._discovery_identity_widths = (discovery_session._candidate, MappingProxyType(identity_uppers))
        widths = await self._discovery_publication_numeric_widths(constructor=constructor)
        union_header_upper = sum(amount for appearance, amount in selected_trace
            if type(appearance) is tuple and appearance[:1] == ("programme-identity-headers",))
        projected_trace = tuple((("programme-identity-headers", identity_ids), union_header_upper)
            if type(appearance) is tuple and appearance[:1] == ("programme-identity-headers",)
            else (appearance, amount) for appearance, amount in trace)
        entries = list(_discovery_projected_superset_charges(certificate,
            projected_trace, job_id=constructor["job_id"],
            row_upper=constructor["row_header_bytes"]))
        # The real combined Identity certificate emits one full-header charge.
        # Sum both genuine owner measurements before replacing only its numeric
        # appearance label. No selection or certificate is reconstructed.
        selected_entries = list(_discovery_projected_superset_charges(certificate,
            selected_trace, job_id=constructor["job_id"], row_upper=constructor["row_header_bytes"]))
        identity_headers = [(index, amount) for index, (appearance, amount) in enumerate(selected_entries)
            if type(appearance) is tuple and appearance[:1] == ("programme-identity-headers",)]
        if identity_headers:
            first = identity_headers[0][0]
            total = sum(amount for _, amount in identity_headers)
            indices = {index for index, _ in identity_headers}
            selected_entries = [(("programme-identity-headers", identity_ids), total) if index == first
                else entry for index, entry in enumerate(selected_entries) if index == first or index not in indices]
        entries.extend(selected_entries)
        def row(table, key, upper):
            entries.extend((appearance, upper) for appearance in (
                ("programme-row-output", "dict-copy", table, key),
                ("programme-row-output", "encoded-copy", table, key), ("body", table, key)))
        job_id, upper = constructor["job_id"], constructor["row_header_bytes"]
        row("workflow_run_states", job_id, upper)
        for appearance in ("authority-json", "authority-model", "composition-json",
                "composition-model", "composition-dependencies", "authority-digest-json",
                "authority-digest-canonical", "authority-digest-encoded", "input-digest-json",
                "input-digest-canonical", "input-digest-encoded", "run-map-entry"):
            entries.append((("programme-selector", appearance, "workflow_run_states", job_id), upper))
        goal, issuer = binding.goal_id, binding.issuer_root_id
        goal_upper = certificate.rows[("goals", goal)][1]
        issuer_upper = certificate.rows[("operator_sessions", issuer)][1]
        row("goals", goal, goal_upper)
        for appearance in ("programme-namespace", "programme-json", "programme-envelope",
                "generation-list", "generation-model", "programme-binding-model", "goal-map-entry"):
            entries.append((("programme-selector", appearance, "goals", goal), goal_upper))
        row("operator_sessions", issuer, issuer_upper)
        for appearance in ("issuer-map-entry", "identity-set-entry"):
            entries.append((("programme-selector", appearance, "operator_sessions", issuer), issuer_upper))
        for ancestor in widths["ancestor_goals"]:
            ancestor_upper = certificate.rows[("goals", ancestor)][1]
            row("goals", ancestor, ancestor_upper)
            entries.append((("programme-selector", "ancestor-map-entry", "goals", ancestor), ancestor_upper))
        identity_upper = 2 + sum(6 * len(key.encode("utf-8")) + 3 for key in identity_ids)
        for appearance in ("identity-order", "identity-return"):
            entries.append((("programme-selector", appearance), identity_upper))
        return tuple(entries)

    async def _discovery_publication_numeric_widths(self, *, constructor):
        """Measure original target/output RESOURCE widths without predicting hashes.

        Reads only authentic current selected facts and unchanged owner records.
        No prospective domain row, planRef, authority or witness is constructed.
        The real publisher independently derives its eventual target and delta.
        """
        from types import MappingProxyType
        from src.memory.composition_headers import snapshot_reads, _validate
        from src.workspace.production import MAX_ACCOUNTING_CHECKPOINT_BYTES, MAX_LIFECYCLE_RECEIPT_BYTES
        prospective = self._prospective_admission
        if (prospective is None or type(constructor) is not MappingProxyType
                or self.db.new or self.db.dirty or self.db.deleted
                or type(self.base) is not dict or self.base.get("schema_version") != 2):
            raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
        connection = await self.db.connection()
        common33 = prospective[0].common33
        def selected_facts(conn):
            self._validate_programme_selection(prospective[0])
            _validate(conn, common33)
            if ("workflow_run_states", constructor["job_id"]) in common33.rows:
                raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
            self._check_original_discovery_incoming(conn, common33, service=prospective[2], host=prospective[3],
                job_id=constructor["job_id"])
            # This numerical forecast belongs to the original certified reader
            # before staging. The fresh insertion/queue writer is a later
            # owner check; no writer permission is issued by these widths.
            if self._selection is None:
                raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
            self._validate_programme_selection(self._selection)
            old = self._selection
            goals = dict(prospective[0].goals)
            direct = tuple(goals)
            pending = list(goals)
            while pending:
                key = pending.pop()
                parent = goals[key]["parent_id"]
                if parent is not None and parent not in goals:
                    if parent in old.goals:
                        goals[parent] = old.goals[parent]
                    else:
                        with snapshot_reads(common33):
                            _descriptor, row, raw = _programme_reference_row_on_connection(conn, "goals", parent)
                        # This retained fact copy is distinct from its paid read.
                        self.header_budget.debit(len(raw), appearance=("discovery-ancestor-fact-copy", parent))
                        goals[parent] = MappingProxyType(row)
                    pending.append(parent)
            for key in goals:
                seen, current = set(), key
                while current is not None:
                    if current in seen or current not in goals:
                        raise ProductionWorkspaceReconciliationError("programme_goal_parent_cycle")
                    seen.add(current)
                    current = goals[current]["parent_id"]
            added_goals = tuple(key for key in goals if key not in old.goals)
            added_issuers = tuple(key for key in prospective[0].issuers if key not in old.issuers)
            added_identities = tuple(key for key in prospective[0].identity_ids if key not in old.identity_ids)
            added = (("workflow_run_states", constructor["job_id"]),
                *(("goals", key) for key in added_goals),
                *(("operator_sessions", key) for key in added_issuers),
                *(("operator_identities", key) for key in added_identities))
            if len(self.members | set(added)) > 128:
                raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
            return goals, direct, added_goals, added_issuers, added_identities, added
        goals, direct, added_goals, added_issuers, added_identities, added = await connection.run_sync(selected_facts)
        from src.workspace.production import _publication_json_upper
        encoded = _publication_json_upper
        def object_width(widths):
            return 2 + sum(encoded(key) + 1 + size for key, size in widths.items()) + max(0, len(widths) - 1)
        def modified_width(actual, changes):
            return object_width({key: changes.get(key, encoded(value)) for key, value in actual.items()})
        def count_width(actual, additions):
            widths = {key: encoded(value + additions.get(key, 0)) for key, value in actual.items()}
            widths.update((key, encoded(value)) for key, value in additions.items() if key not in actual and value)
            return object_width(widths)
        native = self.base["native"]
        native_width = modified_width(native, {"total_rows": encoded(native["total_rows"] + 1),
            "table_counts": count_width(native["table_counts"], {"workflow_run_states": 1})})
        prior_programme = self.base["programme"]
        rows = prior_programme["row_counts"] if prior_programme is not None else {}
        relations = prior_programme["relation_counts"] if prior_programme is not None else {}
        row_additions = {"goals": len(added_goals), "operator_sessions": len(added_issuers),
            "operator_identities": len(added_identities)}
        relation_additions = {"programme_goal": 1, "programme_issuer_provenance": 1,
            "goal_parent_fk": sum(goals[key]["parent_id"] is not None for key in added_goals),
            "programme_owner_identity": len(added_issuers)}
        programme_width = object_width({"manifest_version": encoded(PROGRAMME_MANIFEST_VERSION),
            "manifest_digest": encoded(PROGRAMME_MANIFEST_DIGEST),
            "row_counts": count_width(rows, row_additions), "closure_digest": 66,
            "relation_counts": count_width(relations, relation_additions), "relation_digest": 66,
            "artifact_count": encoded((prior_programme["artifact_count"] if prior_programme is not None else 0) + 2),
            "artifact_manifest_digest": 66})
        target_width = modified_width(self.base, {"native": native_width, "programme": programme_width})
        delta_widths = [object_width({"table_id": encoded(table), "key": encoded(key),
            "before_digest": 66, "after_digest": 66}) for table, key in sorted(added)]
        delta_width = 2 + sum(delta_widths) + max(0, len(delta_widths) - 1)
        accounting = await self._publication_accounting_payload(_numeric_body_certificate=common33)
        if accounting.get("operations"):
            raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
        checkpoint_fields = {key: encoded(value) for key, value in accounting.items()}
        checkpoint_fields.update(schema_version=1, composition_base=max(encoded(self.base), target_width),
            composition_target=target_width, composition_delta=delta_width, secret_values_included=5)
        checkpoint_width = object_width(checkpoint_fields)
        prior_receipt = read_lifecycle_receipt(self.workspace, header_budget=self.header_budget)
        receipt = prior_receipt or {"secret_values_included": False}
        receipt_fields = {key: encoded(value) for key, value in receipt.items()}
        receipt_fields["runtime_composition"] = target_width
        if accounting.get("witness") is not None:
            receipt_fields["inference_accounting"] = encoded(accounting["witness"])
        receipt_width = object_width(receipt_fields)
        if checkpoint_width > MAX_ACCOUNTING_CHECKPOINT_BYTES or receipt_width > MAX_LIFECYCLE_RECEIPT_BYTES:
            raise ProductionWorkspaceReconciliationError("composition_publication_size_exceeded")
        # Numeric output/fact storage is a paid appearance, never a witness.
        facts = {"target_bytes": target_width, "native_bytes": native_width, "programme_bytes": programme_width,
            "checkpoint_bytes": checkpoint_width,
            "receipt_bytes": receipt_width, "added_members": added,
            "added_goals": added_goals, "added_issuers": added_issuers, "added_identities": added_identities,
            "direct_goals": direct, "ancestor_goals": tuple(key for key in goals if key not in direct)}
        target_members = self.members | set(added)
        # A's changed membership is a conservative bound for B's one existing
        # root. These are encoded capacity facts; no target row or digest is
        # invented and the publisher still derives the actual touched set.
        target_keys = tuple(sorted(set(added) | {("workflow_run_states", constructor["job_id"])}))
        facts.update(members_bytes=encoded(tuple(sorted(target_members))),
            delta_bytes=delta_width, delta_key_bytes=encoded(target_keys),
            changed_members_bytes=encoded(tuple(sorted(added))),
            changed_touched_bytes=2 + sum(encoded((table, key)) + 1 + 66
                for table, key in target_keys) + max(0, len(target_keys) - 1),
            delta_sort_bytes=encoded(target_keys),
            witness_bytes=encoded(accounting["witness"]) if accounting.get("witness") is not None else 0)
        facts["receipt_default_bytes"] = 0 if prior_receipt else encoded({"secret_values_included": False})
        facts["receipt_target_update_bytes"] = encoded(receipt) + encoded("runtime_composition") + 1 + target_width
        facts["receipt_witness_update_bytes"] = (modified_width(receipt, {"runtime_composition": target_width})
            + encoded("inference_accounting") + 1 + facts["witness_bytes"])
        facts["goal_parents"] = MappingProxyType({key: row["parent_id"] for key, row in goals.items()})
        facts["issuer_identities"] = MappingProxyType({key: row["operator_identity_id"]
            for key, row in prospective[0].issuers.items()})
        identity_widths = getattr(self, "_discovery_identity_widths", None)
        owned = getattr(prospective[2], "_owned_discovery_session", None)
        if (identity_widths is not None and owned is not None
                and identity_widths[0] is owned._candidate and owned._constructor_bounds is constructor):
            facts["identity_row_uppers"] = identity_widths[1]
        self.header_budget.debit(encoded(facts), appearance=("discovery-publication-width-output", constructor["job_id"]))
        return MappingProxyType(facts)

    async def _original_discovery_body_numeric_schedule(self, owned, *, constructor, certificate):
        """Current original snapshot validation and finite table/row fees."""
        from types import MappingProxyType
        from src.memory.composition_headers import snapshot_reads, charge_row, charge_table
        from src.memory.header_bounds import _trace_memory_numeric_charges
        owned.validate(db=self.db, header_budget=self.header_budget)
        if constructor is not owned._constructor_bounds or certificate.budget is not self.header_budget:
            raise ProductionWorkspaceReconciliationError("programme_numeric_body_unavailable")
        connection = await self.db.connection()
        goal_id = owned._candidate[1].goal_id
        with _trace_memory_numeric_charges(self.header_budget) as row:
            def pay_row(conn):
                with snapshot_reads(certificate):
                    charge_row(conn, "goals", goal_id)
            await connection.run_sync(pay_row)
        tables = {}
        for table in ("workflow_run_states", "goals", "sessions"):
            with _trace_memory_numeric_charges(self.header_budget) as trace:
                def pay_table(conn):
                    with snapshot_reads(certificate):
                        charge_table(conn, table)
                await connection.run_sync(pay_table)
            tables[table] = tuple(trace)
        validations = tuple((appearance, upper) for appearance, upper in row
            if appearance != ("body", "goals", goal_id))
        half = len(validations) // 2
        if len(validations) % 2 or validations[:half] != validations[half:]:
            raise ProductionWorkspaceReconciliationError("programme_numeric_validation_trace_changed")
        future = (*validations, (("body", "workflow_run_states", constructor["job_id"]), constructor["row_header_bytes"]))
        return MappingProxyType({"goal": tuple(row), "run": tuple(future),
            "validate": validations[:half], "tables": MappingProxyType(tables)})

    def _original_discovery_publication_output_numeric_schedule(self, owned, *, constructor, publication_widths):
        """The original writers' private reads, emitted copies and exit copies."""
        from src.memory.composition_headers import _metadata_size
        owned.validate(db=self.db, header_budget=self.header_budget)
        if constructor is not owned._constructor_bounds or owned._candidate[0] != constructor["job_id"]:
            raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
        phases = ("queue",) if constructor.get("persisted_queue_only") else ("admission", "queue")
        entries = []
        members = self.members | set(publication_widths["added_members"])
        member_upper = 2 + sum(3 + _metadata_size(table) + _metadata_size(key)
            for table, key in members) + max(0, len(members) - 1)
        leaves = set(self._programme_base_leaves) | {address for address in publication_widths["added_members"]
            if address[0] in self._programme_tables()}
        leaf_upper = 2 + sum(4 + _metadata_size(table) + _metadata_size(key) + 386
            for table, key in leaves) + max(0, len(leaves) - 1)
        delta_keys = set(publication_widths["added_members"]) | {("workflow_run_states", constructor["job_id"])}
        touched_upper = 2 + sum(4 + _metadata_size(table) + _metadata_size(key) + 386
            for table, key in delta_keys)
        for phase in phases:
            label = "publication-" + phase
            for output, key in (("target", "target_bytes"), ("checkpoint", "checkpoint_bytes"), ("receipt", "receipt_bytes")):
                for appearance in ("construction", "canonical", "encoded"):
                    entries.append((label, ("discovery-publication-output", output, appearance), publication_widths[key]))
            for output, key in (("checkpoint", "checkpoint_bytes"), ("receipt", "receipt_bytes")):
                size = publication_widths[key]
                for appearance in ("dict-copy", "json", "utf8", "readback-dict", "readback-json", "readback-utf8"):
                    entries.append((label, ("composition-publication", output, appearance), size))
                # Readback reads raw bytes without a JSON parser.
                entries.append((label, None, size + 1))
            # Original checkpoint writer reads its prior checkpoint and, for
            # schema 2, its paired receipt. Original receipt writer reads prior
            # receipt again. The conditional unused allowance stays spent.
            prior = max(publication_widths["checkpoint_bytes"], publication_widths["receipt_bytes"],
                publication_widths.get("prior_private_read_bytes", 0))
            for output, repeats in (("checkpoint", 1), ("receipt", 2)):
                for _ in range(repeats):
                    entries.append((label, None, prior + 1))
                    if output == "receipt":
                        entries.append((label, ("composition-publication", output, "decode"), prior))
                    for appearance in ("json-parse", "materialization", "return"):
                        entries.append((label, ("composition-publication", output, appearance), 6 * prior + 2))
            entries.append((label, ("discovery-publication-touched-copy", phase, constructor["job_id"]), touched_upper))
            for appearance, upper in (("members", member_upper), ("base-leaves", leaf_upper), ("current-leaves", leaf_upper)):
                entries.append((label, ("discovery-committed-continuation", phase, constructor["job_id"], appearance), upper))
        return tuple(entries)

    async def _original_discovery_continuation_numeric_schedule(self, owned, *, constructor, strategy):
        """Compose original-owner appearances; never manufacture future authority.

        Measurements spend the existing frame. Reservation issuance remains in
        the two original reserve entrypoints, after this complete source census.
        Current and projected repeats remain separate and never refund slack.
        """
        from types import MappingProxyType
        from src.memory.header_bounds import (_trace_memory_numeric_charges,
            _discovery_projected_superset_charges, HeaderBoundsError, MAX_BYTES)
        from src.guardian.goal_programmes import original_discovery_authority_numeric_schedule
        from src.memory.task_methods import original_discovery_strategy_numeric_schedule
        from src.guardian.goal_discovery import (original_discovery_staging_numeric_schedule,
            original_discovery_admission_numeric_schedule)
        from src.workflows.job_runtime import original_discovery_jobs_numeric_schedule
        from src.workflows.research_sources import original_discovery_physical_numeric_schedule
        from src.workflows.inference_accounting import original_discovery_native_binding_numeric_schedule
        from src.workflows import research_guard

        owned.validate(db=self.db, header_budget=self.header_budget)
        if (owned.guard is not self or constructor is not owned._constructor_bounds
                or type(constructor) is not MappingProxyType or owned._candidate_phase != "reader"
                or owned._candidate is None or owned._candidate[0] != constructor["job_id"]
                or self._discovery_reservation is not None or self.db.new or self.db.dirty or self.db.deleted):
            raise HeaderBoundsError("programme_numeric_schedule_owner_changed")
        persisted = constructor.get("persisted_queue_only") is True
        if persisted != (self._prospective_admission is None):
            raise HeaderBoundsError("programme_numeric_schedule_owner_changed")

        # Require the original parser/codec schedules as well as table bounds.
        if not hasattr(research_guard, "original_discovery_research_guard_numeric_schedule"):
            raise HeaderBoundsError("programme_numeric_research_guard_schedule_unavailable")
        native_projection = globals().get("_original_native_closure_projected_entries")
        if not callable(native_projection):
            raise HeaderBoundsError("programme_numeric_native_closure_schedule_unavailable")

        certificate, common, closure, physical = await self._original_discovery_numeric_baseline(
            owned, constructor=constructor)
        authority = await original_discovery_authority_numeric_schedule(owned, constructor=constructor)
        method = await original_discovery_strategy_numeric_schedule(owned,
            constructor=constructor, strategy=strategy)
        with _trace_memory_numeric_charges(self.header_budget) as publication_trace:
            widths = (await self._original_discovery_queue_publication_widths(owned,
                constructor=constructor, certificate=certificate) if persisted else
                await self._discovery_publication_numeric_widths(constructor=constructor))
        widths = MappingProxyType({**widths, "prior_private_read_bytes": max(
            (max(0, upper - 1) for appearance, upper in publication_trace if appearance is None), default=0)})
        bodies = await self._original_discovery_body_numeric_schedule(owned,
            constructor=constructor, certificate=certificate)
        entries = []
        def append(phase, trace):
            entries.extend((phase, appearance, upper) for appearance, upper in trace)
        def project(trace):
            return tuple(trace) if persisted else _discovery_projected_superset_charges(
                certificate, trace, job_id=constructor["job_id"], row_upper=constructor["row_header_bytes"])
        def rephase(phase, schedule):
            entries.extend((phase, appearance, upper) for _, appearance, upper in schedule)

        entries.extend(authority)
        entries.extend(method)
        entries.extend(await original_discovery_jobs_numeric_schedule(owned, constructor=constructor))
        entries.extend(original_discovery_physical_numeric_schedule(owned, constructor=constructor))
        if not persisted:
            entries.extend(original_discovery_staging_numeric_schedule(owned, constructor=constructor))
            entries.extend(original_discovery_admission_numeric_schedule(owned, constructor=constructor))
            with _trace_memory_numeric_charges(self.header_budget) as outstanding:
                await owned.service._outstanding(owned.db, owned._candidate[1], discovery_session=owned)
            append("admission", outstanding)
            # Four direct common reads plus the original first-writer read.
            for _ in range(5):
                append("admission", common)
            append("admission", closure)
            for table in ("workflow_run_states", "goals", "sessions"):
                append("admission", bodies["tables"][table])
            append("admission", bodies["goal"])
        # Six direct B reads plus its genuine first-writer closure certificate.
        for _ in range(7):
            append("queue", project(common))
        for _ in range(4):
            append("queue", bodies["run"])
        append("queue", bodies["goal"])
        # Existing-root CAS touches independently derive the original leaf.
        append("queue", bodies["run"])
        append("queue", _original_native_row_digest_entries("workflow_run_states",
            constructor["job_id"], constructor["row_header_bytes"]))

        publications = ("publication-queue",) if persisted else (
            "publication-admission", "publication-queue")
        aggregates = {"closure-prefix", "relations-prefix", "artifacts-prefix", "leaf-sort",
            "leaf-map-update", "relations-sort", "relations-json", "relations-utf8", "relations-update",
            "files-sort", "files-json", "files-utf8", "files-update", "result", "numeric-facts"}
        future_closure = native_projection(project(closure), constructor=constructor,
            publication_widths=widths)
        if not persisted:
            future_closure = tuple((appearance, upper) for appearance, upper in future_closure
                if not (type(appearance) is tuple and len(appearance) > 1
                    and appearance[0] == "programme-manifest" and appearance[1] in aggregates))
        for phase in ("queue", *publications):
            append(phase, future_closure)
            if not persisted:
                # Source-owned persisted selector/Identity union, not a future
                # selection. Preserve all original measured old-root charges.
                append(phase, tuple((appearance, upper) for selected_phase, appearance, upper
                    in authority if selected_phase == "queue"))
                rephase(phase, original_discovery_manifest_numeric_schedule(owned,
                    constructor=constructor, certificate=certificate, publication_widths=widths))
                append(phase, original_discovery_native_binding_numeric_schedule(owned, constructor=constructor))
                # The native closure's two new charge_row calls each validate
                # the genuine snapshot. Its existing outer validation remains
                # in the actual baseline trace; the codec owner adds body fees.
                for _ in range(2):
                    append(phase, bodies["validate"])
                # The programme component has its own original raw-row
                # delivery after the persisted selector's separate delivery.
                append(phase, bodies["run"])
                append(phase, tuple((("programme-row-output", appearance,
                    "workflow_run_states", constructor["job_id"]), constructor["row_header_bytes"])
                    for appearance in ("dict-copy", "encoded-copy")))
                rephase(phase, original_discovery_physical_numeric_schedule(owned,
                    constructor=constructor, publication=True))
        for phase in publications:
            append(phase, project(common))
            # Delta publication derives the touched WRS leaf independently
            # after the closure's own leaf. Preserve that additional read/hash.
            append(phase, bodies["run"])
            append(phase, _original_native_row_digest_entries("workflow_run_states",
                constructor["job_id"], constructor["row_header_bytes"]))
            # Current private files may contain more whitespace than future
            # canonical output. Retain both measured and future parser bounds.
            append(phase, tuple((appearance, upper) for appearance, upper in publication_trace
                if not (type(appearance) is tuple and appearance[:1] == ("discovery-publication-width-output",))))
            prior = max(widths["checkpoint_bytes"], widths["receipt_bytes"], widths["prior_private_read_bytes"])
            for output, repeats in (("checkpoint", 1), ("receipt", 2)):
                for _ in range(repeats):
                    append(phase, ((None, prior + 1),))
                    if output == "receipt":
                        append(phase, ((("composition-publication", output, "decode"), prior),))
                    append(phase, tuple((("composition-publication", output, label), 6 * prior + 2)
                        for label in ("json-parse", "materialization", "return")))
            rephase(phase, original_discovery_publication_construction_numeric_schedule(owned,
                constructor=constructor, publication_widths=widths))
        entries.extend(self._original_discovery_publication_output_numeric_schedule(owned,
            constructor=constructor, publication_widths=widths))
        # This source-owner schedule accompanies its actual parser precharges;
        # missing codecs are never implicit allowances.
        entries.extend(research_guard.original_discovery_research_guard_numeric_schedule(
            owned, constructor=constructor))
        if any(phase not in {"staging", "admission", "queue", *publications}
                or (appearance is not None and type(appearance) is not tuple)
                or type(upper) is not int or not 0 <= upper <= MAX_BYTES
                for phase, appearance, upper in entries):
            raise HeaderBoundsError("programme_numeric_schedule_invalid")
        owned.validate(db=self.db, header_budget=self.header_budget)
        return tuple(entries), widths, physical

    async def _original_discovery_numeric_baseline(self, owned, *, constructor):
        """Read the real current closure once and retain numerical costs only."""
        from types import MappingProxyType
        from src.memory.header_bounds import _trace_memory_numeric_charges
        owned.validate(db=self.db, header_budget=self.header_budget)
        if (constructor is not owned._constructor_bounds or self._discovery_reservation is not None
                or self.db.new or self.db.dirty or self.db.deleted):
            raise ProductionWorkspaceReconciliationError("programme_numeric_baseline_unavailable")
        with _trace_memory_numeric_charges(self.header_budget) as headers:
            common = await self._original_discovery_common(owned)
        connection = await self.db.connection()
        # The prospective certificate remains the original selection's owner;
        # the newly paid current certificate does not replace that authority.
        certificate = (self._prospective_admission[0].common33
            if self._prospective_admission is not None else common)
        with _trace_memory_numeric_charges(self.header_budget) as closure:
            observed, members = await connection.run_sync(
                lambda conn: self._programme_snapshot(conn, certificate, self.base))
        if observed != self.base or members != self.members:
            raise ProductionWorkspaceReconciliationError("programme_numeric_baseline_conflict")
        physical = getattr(self, "_discovery_current_physical_widths", None)
        if type(physical) is not dict:
            raise ProductionWorkspaceReconciliationError("programme_numeric_baseline_unavailable")
        from src.workspace.production import _publication_json_upper
        self.header_budget.debit(_publication_json_upper(physical),
            appearance=("discovery-physical-capacity-copy", constructor["job_id"]))
        return certificate, tuple(headers), tuple(closure), MappingProxyType(dict(physical))

    async def _original_discovery_queue_publication_widths(self, owned, *, constructor, certificate):
        """Current accepted-root output capacities without a new admission."""
        from types import MappingProxyType
        from src.workspace.production import _publication_json_upper as width
        owned.validate(db=self.db, header_budget=self.header_budget)
        if (constructor is not owned._constructor_bounds or not constructor.get("persisted_queue_only")
                or owned._candidate_phase != "reader" or self._prospective_admission is not None
                or constructor["job_id"] not in self._selection.runs):
            raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
        self._validate_programme_selection(self._selection)
        def object_width(fields):
            return 2 + sum(width(key) + 1 + upper for key, upper in fields.items()) + max(0, len(fields) - 1)
        job_id = constructor["job_id"]
        keys = (("workflow_run_states", job_id),)
        delta = 2 + object_width({"table_id": width("workflow_run_states"),
            "key": width(job_id), "before_digest": 66, "after_digest": 66})
        target = width(self.base)
        accounting = await self._publication_accounting_payload(
            _numeric_body_certificate=certificate, discovery_session=owned)
        if accounting.get("operations"):
            raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
        checkpoint_fields = {key: width(value) for key, value in accounting.items()}
        checkpoint_fields.update(schema_version=1, composition_base=target,
            composition_target=target, composition_delta=delta, secret_values_included=5)
        prior = read_lifecycle_receipt(self.workspace, header_budget=self.header_budget)
        receipt = prior or {"secret_values_included": False}
        receipt_fields = {key: width(value) for key, value in receipt.items()}
        receipt_fields["runtime_composition"] = target
        witness = width(accounting["witness"]) if accounting.get("witness") is not None else 0
        if witness:
            receipt_fields["inference_accounting"] = witness
        target_update = width(receipt) + width("runtime_composition") + 1 + target
        witness_update = object_width({**{key: width(value) for key, value in receipt.items()},
            "runtime_composition": target}) + width("inference_accounting") + 1 + witness
        facts = {"target_bytes": target, "native_bytes": width(self.base["native"]),
            "programme_bytes": width(self.base["programme"]), "checkpoint_bytes": object_width(checkpoint_fields),
            "receipt_bytes": object_width(receipt_fields), "added_members": (),
            "members_bytes": width(tuple(sorted(self.members))), "delta_bytes": delta,
            "delta_key_bytes": width(keys), "delta_sort_bytes": width(keys),
            "changed_members_bytes": 2, "changed_touched_bytes": 2,
            "witness_bytes": witness, "receipt_default_bytes": 0 if prior else width(receipt),
            "receipt_target_update_bytes": target_update, "receipt_witness_update_bytes": witness_update}
        self.header_budget.debit(width(facts), appearance=("discovery-publication-width-output", job_id))
        return MappingProxyType(facts)

    async def _reserve_original_discovery_admission(self, *, discovery_session, programme,
            binding, identifier, now, deadline, strategy, composition_binding, constructor):
        """Reserve the original candidate's continuation before its first stage."""
        from types import MappingProxyType
        from src.guardian.goal_discovery import _OwnedDiscoverySession
        from src.memory.header_bounds import _DiscoveryNumericReservation, HeaderBoundsError
        if type(discovery_session) is not _OwnedDiscoverySession:
            raise HeaderBoundsError("programme_original_session_unavailable")
        owned = discovery_session.validate(db=self.db, header_budget=self.header_budget)
        facts = self._prospective_admission_facts
        if (owned.guard is not self or owned._candidate_phase != "reader"
                or owned._candidate is None or self._discovery_reservation is not None
                or facts is None or binding != facts[2] or owned._candidate[1] != binding
                or programme.id != binding.programme_id
                or programme.goal_revision != binding.goal_revision
                or programme.grant_revision != binding.grant_revision
                or constructor is not owned._constructor_bounds
                or type(constructor) is not MappingProxyType
                or constructor.get("persisted_queue_only")
                or composition_binding is not owned._composition_binding
                or constructor["job_id"] != owned._candidate[0]
                or owned._candidate[0] != "goal-discovery:" + identifier.hex
                or self.db.new or self.db.dirty or self.db.deleted):
            raise HeaderBoundsError("programme_constructor_numeric_owner_changed")
        # Widths were produced by the actual typed constructor owner from these
        # exact scalars; the guard neither reconstructs a spec nor issues a bind.
        if deadline <= now or deadline > programme.expires_at:
            raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
        from src.workspace.accounting_continuity import _derive_programme_admission_rows
        common = self._prospective_admission[0].common33
        connection = await self.db.connection()
        def rederive(conn):
            self._validate_programme_selection(self._prospective_admission[0])
            def fail(code):
                raise HeaderBoundsError(code)
            return _derive_programme_admission_rows(conn, common, goal_id=binding.goal_id,
                programme_id=binding.programme_id, grant_revision=binding.grant_revision, fail=fail)
        current_programme, current_binding, current_goals, current_issuers = await connection.run_sync(rederive)
        if (current_programme != programme or current_binding != binding
                or current_goals != facts[3] or current_issuers != facts[4]):
            raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
        entries, widths, physical = await self._original_discovery_continuation_numeric_schedule(
            owned, constructor=constructor, strategy=strategy)
        from src.memory.header_bounds import WRS_BY_RUN
        # Enroll the actual source-owned future address on the existing frame.
        # This is capacity only; the real insertion and later full headers must
        # still validate every scalar and resolve the original physical rowid.
        self.header_budget.reserve_future_row(WRS_BY_RUN, owned._candidate[0], constructor["row_header_bytes"])
        reservation = _DiscoveryNumericReservation(self, owned, constructor, entries)
        reservation.publication_widths = widths
        reservation.physical_widths = physical
        self._discovery_reservation = reservation
        reservation._checked()
        return MappingProxyType({"composition_binding": composition_binding, "constructor": constructor})

    async def _reserve_original_discovery_queue(self, *, discovery_session, job_id):
        """Accepted siblings reserve only their original persisted B/C path."""
        from src.guardian.goal_discovery import _OwnedDiscoverySession
        from src.memory.header_bounds import _DiscoveryNumericReservation, HeaderBoundsError
        from src.runtime_plugins.ownership import RuntimeCompositionBinding
        from src.workflows.job_runtime import original_discovery_queue_numeric_bounds
        from src.memory.composition_headers import snapshot_reads
        if type(discovery_session) is not _OwnedDiscoverySession:
            raise HeaderBoundsError("programme_original_session_unavailable")
        owned = discovery_session.validate(db=self.db, header_budget=self.header_budget)
        if (owned.guard is not self or owned._candidate_phase != "reader"
                or owned._candidate is None or owned._candidate[0] != job_id
                or self._discovery_reservation is not None
                or self.db.new or self.db.dirty or self.db.deleted):
            raise HeaderBoundsError("programme_original_queue_candidate_changed")
        constructor = await original_discovery_queue_numeric_bounds(owned)
        common = await self._original_discovery_common(owned)
        connection = await self.db.connection()
        def retained_binding(conn):
            with snapshot_reads(common):
                _, row, _ = _programme_reference_row_on_connection(conn, "workflow_run_states", job_id)
            if row["status"] != "accepted" or row["job_kind"] != "goal_public_discovery_v1":
                raise HeaderBoundsError("programme_original_queue_row_changed")
            from src.workflows.inference_accounting import (_accounting_prepay,
                _original_native_binding_numeric_entries)
            text = row["composition_binding_json"]
            if type(text) is not str:
                raise HeaderBoundsError("programme_original_queue_row_changed")
            size = sum(1 if ord(char) < 128 else 2 if ord(char) < 2048
                else 3 if ord(char) < 65536 else 4 for char in text)
            _accounting_prepay(self.header_budget, _original_native_binding_numeric_entries(job_id, size))
            return RuntimeCompositionBinding.from_json(text)
        composition_binding = await connection.run_sync(retained_binding)
        strategy = getattr(owned, "_discovery_persisted_strategy", None)
        if strategy is None:
            raise HeaderBoundsError("programme_original_queue_pin_unavailable")
        owned._constructor_bounds, owned._composition_binding = constructor, composition_binding
        self._finish_programme_admission_selection()
        entries, widths, physical = await self._original_discovery_continuation_numeric_schedule(
            owned, constructor=constructor, strategy=strategy)
        reservation = _DiscoveryNumericReservation(self, owned, constructor, entries)
        reservation.publication_widths = widths
        reservation.physical_widths = physical
        self._discovery_reservation = reservation
        reservation._checked()
        return reservation

    def _finish_programme_admission_selection(self):
        """Original insertion/unwind ends the capacity-only prospective lifetime."""
        self._prospective_admission = None
        self._prospective_admission_facts = None
        self._prospective_admission_finished = True

    async def _complete_original_discovery_writer_exit(self, discovery_session, phase, job_id):
        """Continue the same guard only after the original engine's real commit.

        Publication and the actual commit callback must have witnessed the same
        live engine exit. Neither a caller's success flag nor budget possession
        can substitute for that completed original writer.
        """
        from src.db.engine import _get_original_discovery_exit_completion
        from src.guardian.goal_discovery import _OwnedDiscoverySession
        from src.memory.composition_headers import _metadata_size
        if type(discovery_session) is not _OwnedDiscoverySession:
            raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
        discovery_session.validate(db=self.db, header_budget=self.header_budget)
        completed = _get_original_discovery_exit_completion(discovery_session, phase, job_id)
        if (phase not in {"admission", "queue"} or discovery_session.guard is not self
                or self._discovery_publication_exit is not completed
                or self._discovery_commit_exit is not completed
                or self._discovery_publication_snapshot is None or self.prepared_commit
                or self.db.in_transaction() or self.db.new or self.db.dirty or self.db.deleted
                or discovery_session._candidate is None
                or discovery_session._candidate[0] != job_id):
            raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
        target, members, leaves, touched = self._discovery_publication_snapshot
        if ("workflow_run_states", job_id) not in touched:
            raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
        if phase == "admission" and (touched[("workflow_run_states", job_id)] is not None
                or not self._prospective_admission_finished
                or discovery_session._candidate_phase != "reader"):
            raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
        if phase == "queue" and discovery_session._candidate_phase not in {"reader", "inserted"}:
            raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
        # Copies used by the continuation are another appearance. This does not
        # refund or replace any metadata/body/output cost of either writer.
        member_upper = 2 + sum(3 + _metadata_size(table) + _metadata_size(key)
            for table, key in members) + max(0, len(members) - 1)
        leaf_upper = 2 + sum(4 + _metadata_size(table) + _metadata_size(key) + _metadata_size(digest)
            for (table, key), digest in leaves.items()) + max(0, len(leaves) - 1)
        for appearance, upper in (("members", member_upper), ("base-leaves", leaf_upper),
                ("current-leaves", leaf_upper)):
            self.header_budget.debit(upper,
                appearance=("discovery-committed-continuation", phase, job_id, appearance))
        self.base, self.members = target, set(members)
        self._programme_base_leaves = dict(leaves)
        self._programme_current_leaves = dict(leaves)
        self.db.info["composition_base_witness"] = target
        self.touched.clear()
        self._pending_events.clear()
        self._selection = None
        self._programme_snapshot_owner = None
        self._programme_writer = None
        self._retention_writer_snapshot = None
        self._retention_read_budget = None
        self._writer_first_metadata_frame = None
        self._discovery_writer_metadata_owner = None
        self._discovery_publication_exit = None
        self._discovery_commit_exit = None
        self._discovery_publication_snapshot = None
        discovery_session._record_writer_exit(phase)
        if phase == "queue":
            self._prospective_admission = None
            self._prospective_admission_facts = None
            self._prospective_admission_finished = False
            if self._discovery_reservation is not None:
                self._discovery_reservation._checked()
                self._discovery_reservation.close()
                self._discovery_reservation = None
            discovery_session._end_candidate()

    async def _begin_original_discovery_writer(self, discovery_session, phase):
        """The original retained connection's fresh paid A or B writer."""
        from sqlalchemy import text
        from src.guardian.goal_discovery import _OwnedDiscoverySession
        from src.memory.composition_headers import certify_composition_superset
        if type(discovery_session) is not _OwnedDiscoverySession:
            raise ProductionWorkspaceReconciliationError("programme_original_writer_unavailable")
        discovery_session.validate(db=self.db, header_budget=self.header_budget)
        if (discovery_session.guard is not self or phase not in {"admission", "queue"}
                or discovery_session._candidate is None or self.db.in_transaction()
                or self.db.in_nested_transaction() or self.db.new or self.db.dirty or self.db.deleted
                or self._discovery_writer_metadata_owner is not None
                or (phase == "admission" and discovery_session._candidate_phase != "reader")
                or (phase == "queue" and discovery_session._candidate_phase not in {"reader", "inserted"})):
            raise ProductionWorkspaceReconciliationError("programme_original_writer_unavailable")
        await self.db.execute(text("BEGIN IMMEDIATE"))
        connection = await self.db.connection()
        if connection is not self.db.info.get("owned_discovery_connection"):
            raise ProductionWorkspaceReconciliationError("programme_original_writer_unavailable")
        certificate = await certify_composition_superset(self.db, self.header_budget,
            _header_budget=self.header_budget)
        self._discovery_latest_common = certificate
        self.db.info["native_writer_started"] = True
        self.db.info["composition_writer_owner"] = "durable_jobs"
        await connection.run_sync(lambda current: self._capture_native_writer_snapshot(
            current, self.header_budget, "durable_jobs"))
        self._discovery_writer_metadata_owner = discovery_session
        return certificate

    async def _original_discovery_common(self, discovery_session):
        """Current common33 capacity, never a cached pre-mutation certificate."""
        from sqlalchemy import text
        from src.guardian.goal_discovery import _OwnedDiscoverySession
        from src.memory.composition_headers import certify_composition_superset
        if type(discovery_session) is not _OwnedDiscoverySession:
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        discovery_session.validate(db=self.db, header_budget=self.header_budget)
        connection = await self.db.connection()
        if (discovery_session.guard is not self
                or connection is not self.db.info.get("owned_discovery_connection")
                or self.db.in_nested_transaction()):
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        if not (await connection.get_raw_connection()).driver_connection.in_transaction:
            await self.db.execute(text("BEGIN"))
        certificate = await certify_composition_superset(self.db, self.header_budget,
            _header_budget=self.header_budget)
        self._discovery_latest_common = certificate
        return certificate

    async def _finish_owned_discovery_read(self, discovery_session, job_id):
        """Current persisted queued/terminal original may finish without a writer."""
        from src.memory.composition_headers import snapshot_reads
        from src.work_board.research_parent import discovery_authority, DISCOVERY_KIND, DISCOVERY_SERVICE
        from src.memory.header_bounds import HeaderBoundsError
        discovery_session.validate(db=self.db, header_budget=self.header_budget)
        if (discovery_session.guard is not self or discovery_session._candidate is None
                or discovery_session._candidate[0] != job_id
                or discovery_session._candidate_phase != "reader"
                or self.db.new or self.db.dirty or self.db.deleted or self.touched
                or self._pending_events or self.prepared_commit):
            raise ProductionWorkspaceReconciliationError("programme_original_selection_unavailable")
        common = await self._original_discovery_common(discovery_session)
        connection = await self.db.connection()
        def original_row(current):
            with snapshot_reads(common):
                _descriptor, row, _raw = _programme_reference_row_on_connection(
                    current, "workflow_run_states", job_id)
            upper = common.rows[("workflow_run_states", job_id)][1]
            # Declared-authority decoding and its typed/output comparison are
            # distinct from the fully paid raw appearance above.
            for appearance in ("authority-parse", "authority-model", "binding-copy"):
                common.budget.debit(upper, appearance=("discovery-read-completion", appearance, job_id))
            authority = discovery_authority(row["declared_authority_json"])
            if (row["run_identity"] != job_id or row["job_kind"] != DISCOVERY_KIND
                    or row["owner_kind"] != "service" or row["service_id"] != DISCOVERY_SERVICE
                    or row["owner_principal_id"] != DISCOVERY_SERVICE
                    or authority.original_job_id != job_id
                    or authority.programme_binding != discovery_session._candidate[1]
                    or row["status"] not in {"queued", "succeeded", "failed", "cancelled", "degraded", "expired"}):
                raise HeaderBoundsError("programme_original_read_completion_unavailable")
        await connection.run_sync(original_row)
        self._finish_programme_admission_selection()
        discovery_session._candidate_phase = "read_complete"
        discovery_session._end_candidate()
        self._prospective_admission_finished = False

    async def _finish_original_discovery_owner(self, discovery_session):
        """Close the authentic idle pass without publishing an extra writer."""
        from src.guardian.goal_discovery import _OwnedDiscoverySession
        if type(discovery_session) is not _OwnedDiscoverySession:
            raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
        discovery_session.validate(db=self.db, header_budget=self.header_budget)
        if (discovery_session.guard is not self or discovery_session._candidate is not None
                or discovery_session._candidate_phase is not None
                or self._discovery_reservation is not None
                or self.db.new or self.db.dirty or self.db.deleted or self.touched
                or self._pending_events or self.prepared_commit
                or self._discovery_publication_exit is not None
                or self._discovery_commit_exit is not None):
            raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
        if self.db.in_transaction():
            await self.db.rollback()
        return True

    def _select_programmes(self, connection, common33):
        import asyncio
        import threading
        from types import MappingProxyType
        from src.workspace.accounting_continuity import _ProgrammeSelection, _derive_programme_rows
        def fail(code):
            raise ProductionWorkspaceReconciliationError(code)
        if (self.db.info.get("composition_guard") is not self or self._retention_closed
                or common33.raw_owner is not None or common33.connection is not connection
                or common33.budget is not self.header_budget
                or self.db.sync_session.connection() is not connection):
            fail("programme_original_selection_unavailable")
        goals, issuers, runs, identities = _derive_programme_rows(connection, common33, fail=fail)
        selection = _ProgrammeSelection(self, common33, MappingProxyType(goals), MappingProxyType(issuers),
            MappingProxyType(runs), identities)
        object.__setattr__(selection, "issued_id", id(selection))
        self._selection = selection
        self._programme_snapshot_owner = (asyncio.current_task(), threading.get_ident())
        self._validate_programme_selection(selection)
        return selection

    def _programme_snapshot(self, connection, certificate, expected):
        from src.memory.composition_headers import preflight_programme_identity_component, snapshot_reads
        from src.workflows.research_sources import _original_discovery_physical_capacity_scope
        _validate_programme_envelope(expected)
        selection = self._select_programmes(connection, certificate)
        identity = (preflight_programme_identity_component(certificate, original_selection=selection)
            if selection.identity_ids else None)
        with snapshot_reads(certificate):
            native, members = composition_closure(connection, verify_files=native_composition_files,
                programme_selection=selection)
        self._programme_current_leaves = {}
        with _original_discovery_physical_capacity_scope(self.header_budget) as physical_widths:
            programme = _programme_component_on_connection(connection, certificate, selection, identity,
                root=self.workspace.host_root, original_owner=self, _leaf_sink=self._programme_current_leaves)
        self._discovery_current_physical_widths = physical_widths
        from src.workspace.production import _publication_json_upper as width
        envelope_width = (2 + width("schema_version") + 1 + 1
            + width("projection_schema") + 1 + width(COMPOSITION_ENVELOPE_SCHEMA)
            + width("native") + 1 + width(native) + width("programme") + 1 + width(programme)
            + width("transition_ref") + 1 + width(expected["transition_ref"]) + 4)
        leaves_width = 2 + sum(width(item) for item in self._programme_current_leaves) \
            + max(0, len(self._programme_current_leaves) - 1)
        # Union width is computed over authentic current keys without making
        # the returned set or a speculative future membership object.
        union_count = len(members) + sum(item not in members for item in self._programme_current_leaves)
        union_width = (2 + sum(width(item) for item in members)
            + sum(width(item) for item in self._programme_current_leaves if item not in members)
            + max(0, union_count - 1))
        if self.header_budget is not None:
            for stage, upper in (("construction", envelope_width), ("return", envelope_width),
                    ("leaves-set", leaves_width), ("members-union", union_width)):
                self.header_budget.debit(upper, appearance=("programme-snapshot-envelope", stage))
        return _programme_envelope(native, programme, expected["transition_ref"]), members | set(self._programme_current_leaves)

    def _native_writer_fields(self, connection, budget, owner):
        import asyncio
        import threading
        from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
        session_tx = self.db.sync_session.get_transaction()
        if (self._retention_closed or self.db.info.get("composition_guard") is not self
                or type(budget) is not HeaderReadBudget or self.header_budget is not budget
                or owner not in {"native_ingress", "composition_maintenance", "durable_jobs", "finite_service"}
                or self.db.info.get("composition_writer_owner") != owner
                or not self.db.info.get("native_writer_started")
                or session_tx is None or not session_tx.is_active
                or self.db.in_nested_transaction() or connection.closed or connection.invalidated
                or self.db.sync_session.connection() is not connection):
            raise HeaderBoundsError("memory_retention_writer_unavailable")
        connection_tx = connection.get_transaction()
        driver = connection.connection.driver_connection
        if (connection_tx is None or not connection_tx.is_active
                or connection.in_nested_transaction() or not driver.in_transaction
                or asyncio.current_task() is None):
            raise HeaderBoundsError("memory_retention_writer_unavailable")
        return (self.db, asyncio.current_task(), threading.get_ident(), owner, budget,
                connection, session_tx, connection_tx, driver)

    def _capture_native_writer_snapshot(self, connection, budget, owner):
        # Called only by the existing successful BEGIN IMMEDIATE issuer.
        self._retention_writer_snapshot = self._native_writer_fields(connection, budget, owner)

    def _issue_writer_first_metadata_mode(self, connection, budget):
        """Only the genuine first-writer issuer selects its original mode."""
        import inspect
        from src.memory.header_bounds import HeaderBoundsError
        from src.memory.composition_headers import _checked_current_snapshot_scope
        if (inspect.currentframe().f_back.f_code is not _prepare_composition_writer_session.__code__
                or self not in _COMPOSITION_METADATA_GUARDS
                or self in _WRITER_FIRST_METADATA_GUARDS
                or self._writer_first_metadata_mode is not None
                or self._writer_first_metadata_frame is not None):
            raise HeaderBoundsError("memory_retention_writer_unavailable")
        self._validate_native_writer_snapshot(budget, connection)
        scope = _checked_current_snapshot_scope(budget)
        if scope is None:
            raise HeaderBoundsError("memory_snapshot_scope_unavailable")
        self._writer_first_metadata_frame = (scope, budget, self._retention_writer_snapshot)
        self._writer_first_metadata_mode = "original-budget-v1"
        _WRITER_FIRST_METADATA_GUARDS.add(self)

    def _writer_metadata_budget(self, connection=None):
        """Select no default fallback for an issued original first-writer mode."""
        from src.memory.header_bounds import HeaderBoundsError
        from src.memory.composition_headers import _checked_current_snapshot_scope
        if (self not in _COMPOSITION_METADATA_GUARDS or self._retention_closed
                or self.db.info.get("composition_guard") is not self):
            raise HeaderBoundsError("memory_retention_writer_changed")
        if self._discovery_writer_metadata_owner is not None:
            owned = self._discovery_writer_metadata_owner
            owned.validate(db=self.db, header_budget=self.header_budget)
            if owned.guard is not self:
                raise HeaderBoundsError("memory_retention_writer_changed")
            self._validate_native_writer_snapshot(self.header_budget, connection)
            return self.header_budget
        mode, frame = self._writer_first_metadata_mode, self._writer_first_metadata_frame
        if self not in _WRITER_FIRST_METADATA_GUARDS:
            if mode is None and frame is None:
                return None  # Unchanged genuinely unissued original guard.
            raise HeaderBoundsError("memory_retention_writer_changed")
        if (mode != "original-budget-v1" or type(frame) is not tuple or len(frame) != 3
                or frame[1] is not self.header_budget
                or frame[2] is not self._retention_writer_snapshot):
            raise HeaderBoundsError("memory_retention_writer_changed")
        self._validate_native_writer_snapshot(self.header_budget, connection)
        if _checked_current_snapshot_scope(self.header_budget) is not frame[0]:
            raise HeaderBoundsError("memory_snapshot_scope_unavailable")
        return self.header_budget

    @staticmethod
    def _retention_sql_start(statement):
        """Skip SQLite's leading trivia and empty statements, never SQL bodies."""
        offset, size = 0, len(statement)
        while offset < size:
            if statement[offset] in " \t\n\r\f\v\ufeff;":
                offset += 1
            elif statement.startswith("--", offset):
                newline = statement.find("\n", offset + 2)
                if newline < 0:
                    return size
                offset = newline + 1
            elif statement.startswith("/*", offset):
                closing = statement.find("*/", offset + 2)
                if closing < 0:
                    return size
                offset = closing + 2
            else:
                break
        return offset

    @staticmethod
    def _retention_sql_keyword(statement):
        offset = CompositionSessionGuard._retention_sql_start(statement)
        start, size = offset, len(statement)
        if offset == size or not (statement[offset].isascii() and statement[offset].isalpha()):
            return None
        while offset < size:
            char = statement[offset]
            if not (char.isascii() and (char.isalnum() or char in "_$") or ord(char) >= 128):
                break
            offset += 1
        return statement[start:offset].upper()

    def _deny_budgeted_schema_mutation(self, statement):
        # DDL refers to index/trigger names as well as tables. The old Core
        # substring set cannot protect the original complete Memory schema.
        if self.header_budget is None:
            return
        command = self._retention_sql_keyword(statement)
        if command in {"CREATE", "DROP", "ALTER", "REINDEX", "VACUUM", "ATTACH", "DETACH"}:
            raise ProductionWorkspaceReconciliationError("composition_unhooked_bulk_sql")
        if command == "PRAGMA":
            import re
            source = statement[self._retention_sql_start(statement):]
            scalar = re.fullmatch(
                r'PRAGMA[ \t\n\r\f\v]+(?:encoding|(?:main|temp)\.schema_version)[ \t\n\r\f\v]*;?[ \t\n\r\f\v]*',
                source, flags=re.IGNORECASE | re.ASCII)
            metadata = re.fullmatch(
                r'PRAGMA[ \t\n\r\f\v]+(?:table_info|foreign_key_list|index_list|index_info|index_xinfo)'
                r'[ \t\n\r\f\v]*\([ \t\n\r\f\v]*"((?:""|[^"\x00])*)"[ \t\n\r\f\v]*\)'
                r'[ \t\n\r\f\v]*;?[ \t\n\r\f\v]*', source, flags=re.IGNORECASE | re.ASCII)
            identifier = metadata.group(1).replace('""', '"') if metadata else None
            if ('=' in source or not (scalar or (identifier is not None
                    and 0 < len(identifier.encode("utf-8")) <= 128))):
                raise ProductionWorkspaceReconciliationError("composition_unhooked_bulk_sql")

    def _validate_native_writer_snapshot(self, budget, connection=None):
        from src.memory.header_bounds import HeaderBoundsError
        captured = self._retention_writer_snapshot
        if captured is None:
            raise HeaderBoundsError("memory_retention_writer_unavailable")
        connection = captured[5] if connection is None else connection
        actual = self._native_writer_fields(connection, budget, captured[3])
        if any((current != original if index in {2, 3} else current is not original)
               for index, (current, original) in enumerate(zip(actual, captured))):
            raise HeaderBoundsError("memory_retention_writer_changed")

    @contextmanager
    def _retention_reads(self, budget):
        from src.memory.header_bounds import HeaderBoundsError
        self._validate_native_writer_snapshot(budget)
        previous = self._retention_read_budget
        if previous is not None and previous is not budget:
            raise HeaderBoundsError("memory_retention_writer_changed")
        self._retention_read_budget = budget
        try:
            yield
        finally:
            self._retention_read_budget = previous

    def _enroll_legacy_continuity_metadata(self, connection, key):
        """Private canonical legacy transcript caller; no native authorization."""
        row = _sql(connection, 'SELECT id,owner_principal_id,created_at,continuity_task_id FROM sessions WHERE id=?', (key,)).fetchone()
        if row is None or row[3] is None:
            return
        if (self.db.info.get("composition_writer_owner") != "native_ingress"
            or ("sessions", key) in self.members
            or self._native_session_provenance(connection, key)):
            raise ProductionWorkspaceReconciliationError("composition_session_continuity_unsupported")
        self._legacy_continuity_metadata[key] = tuple(row)

    def _native_session_provenance(self, connection, key):
        if _sql(connection, "SELECT 1 FROM workflow_run_states WHERE composition_binding_json IS NOT NULL AND (session_id=? OR conversation_id=?) LIMIT 1", (key, key)).fetchone():
            return True
        return any(getattr(value, "__tablename__", None) == "workflow_run_states"
            and getattr(value, "composition_binding_json", None) is not None
            and key in (getattr(value, "session_id", None), getattr(value, "conversation_id", None))
            for value in (*self.db.sync_session.new, *self.db.sync_session.dirty))

    def _legacy_continuity_metadata_allowed(self, session, connection, value):
        from sqlalchemy import inspect
        key = value.id
        original = self._legacy_continuity_metadata.get(key)
        if original is None:
            return False
        row = _sql(connection, 'SELECT id,owner_principal_id,created_at,continuity_task_id FROM sessions WHERE id=?', (key,)).fetchone()
        if (value in session.new or value in session.deleted or ("sessions", key) in self.members
            or self.db.info.get("composition_writer_owner") != "native_ingress"
            or row is None or tuple(row) != original or self._native_session_provenance(connection, key)
            or any(inspect(value).attrs[field].history.has_changes()
                for field in ("id", "owner_principal_id", "created_at", "continuity_task_id"))):
            raise ProductionWorkspaceReconciliationError("composition_session_continuity_unsupported")
        return True

    def _touch(self, connection, table, key, *, creating=False):
        key = self._address(table, key)
        if (table, key) in self.touched:
            return
        if len(self.touched) >= 128:
            raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
        owner = self.db.info.get("composition_writer_owner")
        if owner not in {"durable_jobs", "native_ingress", "composition_maintenance", "finite_service"}:
            raise ProductionWorkspaceReconciliationError("composition_unhooked_writer")
        self._ensure_programme_writer(connection)
        before = None if creating else self._leaf(connection, table, key, original=True)
        self.touched[(table, key)] = before

    def _check_private_journal(self, before, after, *, run_id, statement=None):
        from src.workflows.job_runtime import _protected_composition_checkpoint
        def protected(raw):
            records = json.loads(raw or "[]")
            if type(records) is not list:
                raise ProductionWorkspaceReconciliationError("composition_private_journal_invalid")
            selected = {}
            for item in records:
                if type(item) is dict and _protected_composition_checkpoint(item.get("checkpoint_id")):
                    identifier = item["checkpoint_id"]
                    if identifier in selected:
                        raise ProductionWorkspaceReconciliationError("composition_private_journal_duplicate")
                    selected[identifier] = item
            return selected
        previous, current = protected(before), protected(after)
        memory_ids = {"memory:original-reference.v2", "memory:current-reference.v2"}
        memory_permission = ()
        if any(previous.get(key) != current.get(key) for key in memory_ids):
            memory_permission = _consume_native_memory_publication(
                self.db, statement, run_id, before, after, previous, current)
        task_ids = {"runtime-service-invocation:task-capability:" + phase
            for phase in ("admission", "source", "invoke", "outcome", "cleanup")}
        if task_ids.intersection(current):
            from src.runtime_plugins.task_capability import preflight_report_journal
            preflight_report_journal(json.loads(after or "[]"))
        family_permission = self.db.info.get("composition_native_turn_family_receipt")
        permitted = (self.db.info.get("composition_native_claim_receipt"), self.db.info.get("composition_native_turn_receipt"), family_permission, *memory_permission)
        if any(previous.get(key) != current.get(key) for key in task_ids):
            from src.runtime_plugins.task_capability import validate_task_publication
            task_permission = validate_task_publication(self.db, previous, current, run_id=run_id,
                previous_journal=json.loads(before or "[]"), current_journal=json.loads(after or "[]"))
            permitted = (*permitted, *task_permission)
        controls = {"conversation:cancel", "conversation:callback-closure"}
        if controls.intersection(current):
            from src.agent.native_turn_controls import preflight_control_journal
            preflight_control_journal(json.loads(after or "[]"))
        control_permission = ()
        if any(previous.get(key) != current.get(key) for key in controls):
            from src.agent.native_turn_controls import validate_control_publication
            control_permission = validate_control_publication(self.db, previous, current, run_id=run_id)
            permitted = (*permitted, *control_permission)
        output_ids = {"inference:owned-output-intent.v1", "inference:owned-output.v1", "inference:original-candidate.v1"}
        if any(previous.get(key) != current.get(key) for key in output_ids):
            from src.runtime_plugins.inference_output import validate_output_publication
            output_permission = validate_output_publication(self.db, previous, current, run_id=run_id)
            permitted = (*permitted, *output_permission)
        from src.agent.native_turn_family import FAMILY_CHECKPOINT_ID, validate_family_transition
        from src.workflows.job_runtime import _digest
        for selected in (previous, current):
            receipt = selected.get(FAMILY_CHECKPOINT_ID)
            if receipt is not None and (set(receipt) != {"checkpoint_id", "state_digest", "safe", "payload"}
                or receipt["safe"] is not True or receipt["state_digest"] != _digest(receipt["payload"])):
                raise ProductionWorkspaceReconciliationError("composition_turn_family_receipt_invalid")
        for identifier, receipt in previous.items():
            if current.get(identifier) != receipt:
                if identifier in memory_ids and current.get(identifier) in memory_permission:
                    continue
                if identifier in controls and current.get(identifier) in control_permission:
                    continue
                if identifier == FAMILY_CHECKPOINT_ID and current.get(identifier) == family_permission:
                    validate_family_transition(receipt["payload"], family_permission["payload"])
                    continue
                raise ProductionWorkspaceReconciliationError("composition_private_journal_mutation_denied")
        for identifier in current.keys() - previous.keys():
            if current[identifier] not in permitted:
                raise ProductionWorkspaceReconciliationError("composition_private_journal_mint_denied")
            if identifier == FAMILY_CHECKPOINT_ID:
                validate_family_transition(None, current[identifier]["payload"])

    def _check_read_context(self, original, value):
        from sqlalchemy import inspect
        from src.runtime_plugins.read_admission import READ_JOB_KIND
        from src.runtime_plugins.read_journal import read_context, validate_context_transition
        if value.job_kind != READ_JOB_KIND and (original is None or original["job_kind"] != READ_JOB_KIND):
            return
        read_context(value)
        if original is not None:
            previous = read_context(SimpleNamespace(**original))
            current = read_context(value)
            if (any(getattr(value, key) != original[key] for key in
                ("job_kind", "capability_version", "input_digest", "owner_principal_id", "operator_session_id", "session_id", "conversation_id", "declared_authority_json"))
                or inspect(value).attrs.deadline_at.history.has_changes()):
                raise ProductionWorkspaceReconciliationError("composition_read_context_mutation_denied")
            validate_context_transition(previous, current)
        if original is None or value.checkpoint_context_json != original["checkpoint_context_json"]:
            if self.db.info.get("composition_native_read_context") != (value.run_identity, value.checkpoint_context_json):
                raise ProductionWorkspaceReconciliationError("composition_read_context_mint_denied")

    def install(self):
        from sqlalchemy import event, select
        from sqlalchemy.sql.elements import TextClause
        def poison(callback):
            def wrapped(*args, **kwargs):
                try:
                    return callback(*args, **kwargs)
                except ProductionWorkspaceReconciliationError as exc:
                    self.db.info["composition_sticky_failure"] = exc
                    raise
            return wrapped
        def before_execute(state):
            if isinstance(state.statement, TextClause):
                self._deny_budgeted_schema_mutation(state.statement.text)
                source = state.statement.text.lower().lstrip()
                if not source.startswith(("select", "begin", "pragma")) and any(table in source for table in self._protected_tables()):
                    raise ProductionWorkspaceReconciliationError("composition_unhooked_bulk_sql")
            if not (state.is_update or state.is_delete):
                return
            statement = state.statement
            table = getattr(statement, "table", None)
            name = getattr(table, "name", None)
            if name not in self._protected_tables():
                return
            values = getattr(statement, "_values", {}) or {}
            if name == "workflow_run_states" and any(
                getattr(column, "name", column) in {"composition_binding_json", "source_task_id"} for column in values
            ):
                raise ProductionWorkspaceReconciliationError("composition_binding_retrofit_denied")
            key = COMPOSITION_KEYS.get(name, "id")
            lookup = select(table.c[key])
            if statement.whereclause is not None:
                lookup = lookup.where(statement.whereclause)
            records = state.session.execute(lookup).scalars()
            connection = state.session.connection()
            for identity in records:
                identity = self._address(name, identity)
                if name == "sessions" and identity in self._legacy_continuity_metadata:
                    raise ProductionWorkspaceReconciliationError("composition_unhooked_bulk_sql")
                if name == "workflow_run_states":
                    original = _composition_row(connection, name, identity)
                    if original["job_kind"] == "runtime_service_read_v1" and any(
                        getattr(column, "name", column) in {"checkpoint_context_json", "job_kind", "input_digest", "declared_authority_json", "owner_principal_id", "operator_session_id", "session_id", "conversation_id", "deadline_at", "capability_version"}
                        for column in values):
                        raise ProductionWorkspaceReconciliationError("composition_read_context_bulk_denied")
                    for column, value in values.items():
                        if getattr(column, "name", column) == "checkpoint_receipts_json":
                            raw = getattr(value, "value", None)
                            if type(raw) is not str:
                                raise ProductionWorkspaceReconciliationError("composition_private_journal_expression_denied")
                            self._check_private_journal(_composition_row(connection, name, identity)["checkpoint_receipts_json"], raw, run_id=identity, statement=statement)
                if (name, identity) in self.members:
                    self._touch(connection, name, identity)
            state.update_execution_options(_composition_tracked_writer=self)
        def connection_started(session, transaction, connection):
            def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                from src.memory.header_bounds import HeaderBoundsError
                self._deny_budgeted_schema_mutation(statement)
                command = self._retention_sql_keyword(statement)
                control = command in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
                if self._retention_read_budget is not None:
                    self._validate_native_writer_snapshot(self._retention_read_budget, conn)
                    if control or command is None:
                        raise HeaderBoundsError("memory_retention_writer_changed")
                if control or command is None:
                    self._retention_writer_snapshot = None
                compiled = getattr(context, "compiled", None)
                operation = getattr(compiled, "statement", None)
                table = getattr(getattr(operation, "table", None), "name", None)
                dml = any(getattr(operation, kind, False) for kind in ("is_insert", "is_update", "is_delete"))
                if compiled is None or isinstance(operation, TextClause):
                    source = statement.lower().lstrip()
                    dml = not source.startswith(("select", "begin", "pragma", "savepoint", "release", "rollback"))
                    protected = dml and any(name in source for name in self._protected_tables())
                else:
                    protected = dml and table in self._protected_tables()
                if protected and not (session._flushing or context.execution_options.get("_composition_tracked_writer") is self):
                    raise ProductionWorkspaceReconciliationError("composition_unhooked_connection_sql")
            guarded = poison(before_cursor_execute)
            event.listen(connection, "before_cursor_execute", guarded)
            self.connection_listeners.append((connection, guarded))
        def before_flush(session, context, instances):
            if self.db.info.get("auth_session_budget") is not None:
                changes = (*session.new, *session.dirty, *session.deleted)
                if changes and (self._session_reservation is None or session.new or session.deleted
                        or any(value is not self._session_instance[0] for value in session.dirty)):
                    raise ProductionWorkspaceReconciliationError("session_mutation_unreserved")
                if changes:
                    self._validate_session_reservation()
            connection = session.connection()
            for value in (*session.new, *session.dirty, *session.deleted):
                table = getattr(value, "__tablename__", None)
                if table not in self._protected_tables():
                    continue
                key = getattr(value, COMPOSITION_KEYS.get(table, "id"))
                if table == "work_board_events" and value in session.new and key is None:
                    self._pending_events.append(value)
                else:
                    key = self._address(table, key)
                related = (table, key) in self.members or table == "runtime_composition_states"
                if table == "workflow_run_states":
                    if value not in session.new:
                        original = _composition_row(connection, table, key)
                        self._check_read_context(original, value)
                        self._check_private_journal(original["checkpoint_receipts_json"], value.checkpoint_receipts_json, run_id=key)
                        if any(getattr(value, field, None) != original[field] for field in ("composition_binding_json", "source_task_id")):
                            raise ProductionWorkspaceReconciliationError("composition_binding_retrofit_denied")
                    else:
                        self._check_read_context(None, value)
                        self._check_private_journal("[]", value.checkpoint_receipts_json, run_id=key)
                    if value.job_kind == "conversation_turn_v1":
                        checked_turn_family({field: getattr(value, field) for field in RETAINED_FIELDS[table]})
                    related = related or getattr(value, "composition_binding_json", None) is not None
                    related = related or any((table, getattr(value, field, None)) in self.members
                        for field in ("parent_job_id", "parent_run_identity", "root_run_identity"))
                if table.startswith("work_board_"):
                    related = related or any(("work_board_tasks", getattr(value, field, None)) in self.members
                        for field in ("task_id", "parent_task_id", "child_task_id"))
                    related = related or ("workflow_run_states", getattr(value, "workflow_run_id", None)) in self.members
                if table == "memory_episodes":
                    related = related or self.db.info.get("composition_writer_owner") == "native_ingress"
                    related = related or ("messages", getattr(value, "source_message_id", None)) in self.members
                if table == "sessions":
                    related = related or self.db.info.get("composition_writer_owner") in {"native_ingress", "durable_jobs"}
                    if self._legacy_continuity_metadata_allowed(session, connection, value):
                        related = False
                if table == "audit_events":
                    related = related or getattr(value, "event_type", None) == "runtime_composition_recovery"
                    permission = self.db.info.get("composition_native_read_audit")
                    if permission is not None and permission[0] == key:
                        from src.runtime_plugins.read_journal import audit_event_body
                        from src.workflows.job_runtime import _digest
                        if permission[1] != _digest(audit_event_body(value)):
                            raise ProductionWorkspaceReconciliationError("composition_native_read_audit_changed")
                        related = True
                if related:
                    try:
                        if table == "memory_episodes":
                            encode_turn_float(value.salience)
                            encode_turn_float(value.confidence)
                            if value.subject_entity_id is not None or value.project_entity_id is not None:
                                raise ProductionWorkspaceReconciliationError("composition_turn_episode_extension_unsupported")
                        if not (table == "work_board_events" and value in session.new and key is None):
                            self._touch(connection, table, key, creating=value in session.new)
                    except ProductionWorkspaceReconciliationError as exc:
                        self.db.info["composition_sticky_failure"] = exc
                        raise
        def before_commit(session):
            if session.info.get("composition_sticky_failure") is not None:
                raise session.info["composition_sticky_failure"]
            if session.in_nested_transaction():
                # Releasing a savepoint does not publish the outer writer.
                return
            if self.prepared_commit:
                if self._discovery_publication_exit is not None:
                    from src.db.engine import _get_original_discovery_exit_completion
                    owned, phase, job_id, _task = self._discovery_publication_exit
                    if _get_original_discovery_exit_completion(owned, phase, job_id) is not self._discovery_publication_exit:
                        raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
                self.prepared_commit = False
                return
            session.flush()
            if self.touched or session.info.get("composition_accounting_payload"):
                raise ProductionWorkspaceReconciliationError("composition_unpublished_internal_commit")
        def after_flush_postexec(session, context):
            pending, self._pending_events = self._pending_events, []
            for event in pending:
                self._touch(session.connection(), "work_board_events", event.event_id, creating=True)
        def after_commit(session):
            if self._discovery_publication_exit is not None:
                from src.db.engine import _get_original_discovery_exit_completion
                owned, phase, job_id, _task = self._discovery_publication_exit
                completed = _get_original_discovery_exit_completion(owned, phase, job_id)
                if completed is not self._discovery_publication_exit or self.prepared_commit:
                    raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
                self._discovery_commit_exit = completed
        def after_rollback(session):
            self._discovery_publication_exit = None
            self._discovery_commit_exit = None
            self._discovery_publication_snapshot = None
        for name, callback in (("after_flush_postexec", after_flush_postexec), ("do_orm_execute", before_execute), ("before_flush", before_flush),
                               ("after_begin", connection_started), ("before_commit", before_commit),
                               ("after_commit", after_commit), ("after_rollback", after_rollback)):
            guarded = poison(callback)
            event.listen(self.db.sync_session, name, guarded)
            self.listeners.append((name, guarded))

    async def _publication_accounting_payload(self, *, _numeric_body_certificate=None,
            discovery_session=None):
        """Original unchanged ledger/readback predicates, in this same session."""
        if _numeric_body_certificate is not None:
            from src.memory.composition_headers import _validate
            prospective = self._prospective_admission
            if discovery_session is not None:
                from src.guardian.goal_discovery import _OwnedDiscoverySession
                if type(discovery_session) is not _OwnedDiscoverySession:
                    raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
                discovery_session.validate(db=self.db, header_budget=self.header_budget)
                if discovery_session.guard is not self or discovery_session._candidate is None:
                    raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
            elif (prospective is None or _numeric_body_certificate is not prospective[0].common33
                    or _numeric_body_certificate.budget is not self.header_budget
                    or self.db.info.get("composition_guard") is not self):
                raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
            connection = await self.db.connection()
            def validate_numeric_reader(conn):
                if discovery_session is None:
                    self._validate_programme_selection(prospective[0])
                elif (_numeric_body_certificate.connection is not conn
                        or _numeric_body_certificate.budget is not self.header_budget
                        or _numeric_body_certificate.raw_owner is not None):
                    raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
                _validate(conn, _numeric_body_certificate)
            await connection.run_sync(validate_numeric_reader)
        accounting = self.db.info.get("composition_accounting_payload")
        if accounting is None:
            # A composition-only writer must retain the unchanged accounting
            # owner proof. Dropping it would regress the shared checkpoint.
            from sqlalchemy import select
            from src.db.models import InferenceAccountingOwner, InferenceCostReservation
            from src.workflows.inference_accounting import _witness, _ledger_digest
            from src.workspace.production import read_accounting_checkpoint
            prior = read_accounting_checkpoint(self.workspace, header_budget=self.header_budget)
            lifecycle = read_lifecycle_receipt(self.workspace, header_budget=self.header_budget) or {}
            if self.header_budget is not None:
                from src.memory.header_bounds import COMPOSITION_DESCRIPTORS
                from src.memory.composition_headers import locate_exact_rows
                payer = self.header_budget if _numeric_body_certificate is not None else None
                ids = await locate_exact_rows(self.db, COMPOSITION_DESCRIPTORS["inference_accounting_owners"], "deployment",
                    self.header_budget, _header_budget=payer)
                await self.header_budget.certify(self.db, COMPOSITION_DESCRIPTORS["inference_accounting_owners"], ids,
                    _header_budget=payer)
                await self.header_budget.certify_all(self.db, COMPOSITION_DESCRIPTORS["inference_cost_reservations"],
                    _header_budget=payer)
            if _numeric_body_certificate is not None:
                from src.memory.composition_headers import snapshot_reads, charge_row, charge_table
                def pay_owner(conn):
                    with snapshot_reads(_numeric_body_certificate):
                        if ids:
                            charge_row(conn, "inference_accounting_owners", "deployment")
                await connection.run_sync(pay_owner)
            account = await self.db.get(InferenceAccountingOwner, "deployment")
            if _numeric_body_certificate is not None:
                def pay_costs(conn):
                    with snapshot_reads(_numeric_body_certificate):
                        charge_table(conn, "inference_cost_reservations")
                await connection.run_sync(pay_costs)
            rows = list((await self.db.execute(select(InferenceCostReservation))).scalars())
            if account is None:
                if rows or lifecycle.get("inference_accounting") is not None or (prior and prior.get("witness") is not None):
                    raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
                accounting = {}
            else:
                witness = _witness(account, header_budget=self.header_budget if _numeric_body_certificate is not None else None)
                if (account.ledger_digest != _ledger_digest(account, rows,
                    header_budget=self.header_budget if _numeric_body_certificate is not None else None)
                    or lifecycle.get("inference_accounting") != witness
                    or prior is None or prior.get("witness") != witness
                    or prior.get("secret_values_included") is not False
                    or not isinstance(prior.get("account"), dict)):
                    raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
                accounting = {"base": prior.get("base"), "account": prior["account"],
                    "operations": [], "witness": witness, "secret_values_included": False}
        return accounting

    async def _prepare_publication(self):
        from src.workspace.production import _publication_json_upper
        def width(value):
            return 0 if self.header_budget is None else _publication_json_upper(value)
        def pay(appearance, upper):
            if self.header_budget is not None:
                self.header_budget.debit(upper,
                    appearance=("composition-prepare-publication", appearance))
        connection = await self.db.connection()
        def checked_snapshot(conn):
            if self.header_budget is None:
                if type(self.base) is dict and self.base.get("schema_version") == 2:
                    raise ProductionWorkspaceReconciliationError("programme_original_budget_required")
                return composition_closure(conn, verify_files=native_composition_files)
            from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection, snapshot_reads
            certificate = _certify_current_memory_snapshot_on_connection(conn, self.header_budget,
                _header_budget=self._writer_metadata_budget(conn))
            self._publication_common = certificate
            if type(self.base) is dict and self.base.get("schema_version") == 2:
                return self._programme_snapshot(conn, certificate, self.base)
            with snapshot_reads(certificate):
                return composition_closure(conn, verify_files=native_composition_files)
        target, members = await connection.run_sync(checked_snapshot)
        self._publication_members = members
        pay("members-difference", width(members) + width(self.members))
        changed_members = members.symmetric_difference(self.members)
        if changed_members and self.db.info.get("composition_writer_owner") not in {
                "durable_jobs", "native_ingress", "composition_maintenance", "finite_service"}:
            raise ProductionWorkspaceReconciliationError("composition_unhooked_membership_writer")
        pay("delta-key-union", width(self.touched.keys()) + width(changed_members))
        if len(set(self.touched) | changed_members) > 128:
            raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
        changed_touched_upper = 2 + sum(width(table) + width(key) + 70
            for table, key in changed_members) + max(0, len(changed_members) - 1)
        pay("changed-touched-map", changed_touched_upper)
        for table, key in changed_members:
            if (table, key) not in self.touched:
                # Newly retained ancestors must be included in the shared delta.
                self.touched[(table, key)] = None if (table, key) not in self.members else await connection.run_sync(
                    lambda conn, table=table, key=key: self._leaf(conn, table, key, original=True))
        if not self.touched and target != self.base:
            raise ProductionWorkspaceReconciliationError("composition_unhooked_membership_writer")
        delta_sort_upper = 2 + sum(width(table) + width(key) + width(before) + 6
            for (table, key), before in self.touched.items()) + max(0, len(self.touched) - 1)
        delta_upper = 2 + sum(2 + width("table_id") + 1 + width(table)
            + width("key") + 1 + width(key) + width("before_digest") + 67
            + width("after_digest") + 67 + 3 for table, key in self.touched) + max(0, len(self.touched) - 1)
        pay("delta-sort", delta_sort_upper)
        pay("delta-construction", delta_upper)
        delta = []
        for (table, key), before in sorted(self.touched.items()):
            after = await connection.run_sync(lambda conn, table=table, key=key:
                self._leaf(conn, table, key)) if (table, key) in members else None
            delta.append({"table_id": table, "key": key, "before_digest": before, "after_digest": after})
        from src.db.engine import _original_discovery_exit_completion
        owned_exit = _original_discovery_exit_completion.get()
        accounting = await self._publication_accounting_payload(
            _numeric_body_certificate=self._publication_common if owned_exit is not None else None,
            discovery_session=owned_exit[0] if owned_exit is not None else None)
        if target == self.base and not self.db.info.get("composition_accounting_payload"):
            pay("return", width(target) + 12)
            return target, None, None
        if len(delta) + len(accounting.get("operations", [])) > 128:
            raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
        checkpoint_upper = (2 + sum(width(key) + 1 + width(value)
            for key, value in accounting.items() if key not in {"schema_version", "composition_base",
                "composition_target", "composition_delta", "secret_values_included"})
            + width("schema_version") + 2 + width("composition_base") + 1 + width(self.base)
            + width("composition_target") + 1 + width(target)
            + width("composition_delta") + 1 + width(delta)
            + width("secret_values_included") + 6
            + max(0, len(accounting) + sum(name not in accounting for name in
                ("schema_version", "composition_base", "composition_target",
                 "composition_delta", "secret_values_included")) - 1))
        pay("checkpoint-construction", checkpoint_upper)
        checkpoint = {**accounting, "schema_version": 2, "composition_base": self.base,
            "composition_target": target, "composition_delta": delta, "secret_values_included": False}
        prior_receipt = read_lifecycle_receipt(self.workspace, header_budget=self.header_budget)
        if not prior_receipt:
            pay("receipt-default", 2 + width("secret_values_included") + 6)
        receipt = prior_receipt or {"secret_values_included": False}
        pay("receipt-target-update", width(receipt) + width("runtime_composition") + 1 + width(target))
        receipt["runtime_composition"] = target
        if accounting.get("witness") is not None:
            pay("receipt-witness-update", width(receipt) + width("inference_accounting") + 1 + width(accounting["witness"]))
            receipt["inference_accounting"] = accounting["witness"]
        # Check BOTH size bounds before publishing either file.
        from src.workspace.production import MAX_ACCOUNTING_CHECKPOINT_BYTES, MAX_LIFECYCLE_RECEIPT_BYTES
        for label, payload in (("checkpoint", checkpoint), ("receipt", receipt)):
            for appearance in ("size-json", "size-utf8"):
                pay(label + "-" + appearance, width(payload))
        if (len(json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode()) > MAX_ACCOUNTING_CHECKPOINT_BYTES
            or len(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()) > MAX_LIFECYCLE_RECEIPT_BYTES):
            raise ProductionWorkspaceReconciliationError("composition_publication_size_exceeded")
        pay("return", width(target) + width(checkpoint) + width(receipt) + 4)
        return target, checkpoint, receipt

    async def reserve_session_mutation(self, record, changes):
        """Reserve resources for one resolved original Auth mutation, not authority."""
        from sqlalchemy.dialects.sqlite import dialect
        from src.db.models import OperatorSession
        from src.memory.header_bounds import (OPERATOR_SESSION, _SessionNumericReservation,
            _trace_memory_numeric_charges, _native_memory_projected_header_upper, HeaderBoundsError)
        from src.workspace.production import read_accounting_checkpoint
        if (type(record) is not OperatorSession or self._session_reservation is not None
                or self.db.new or self.db.deleted or self.db.dirty
                or not changes or set(changes) - {"last_seen_at", "idle_expires_at", "revoked_at"}):
            raise HeaderBoundsError("session_mutation_unavailable")
        connection = await self.db.connection()
        binding = await connection.run_sync(lambda conn: self._native_writer_fields(conn, self.header_budget, "finite_service"))
        await connection.run_sync(self._ensure_programme_writer)
        selected_dialect = dialect()
        row, original_row = {}, {}
        for name in OPERATOR_SESSION.columns:
            value = changes.get(name, getattr(record, name))
            processor = OperatorSession.__table__.columns[name].type.dialect_impl(selected_dialect).bind_processor(selected_dialect)
            row[name] = processor(value) if processor is not None else value
            old = getattr(record, name)
            original_row[name] = processor(old) if processor is not None else old
        original_upper = _native_memory_projected_header_upper(OPERATOR_SESSION, record.id, original_row)
        resolved = tuple(row.items())
        # Charge numerical forecast storage before constructing target bytes.
        upper = _native_memory_projected_header_upper(OPERATOR_SESSION, record.id, row)
        self.header_budget.debit(upper, appearance=("session-forecast", record.id))
        if ("operator_sessions", record.id) in self.members:
            await connection.run_sync(lambda conn: self._touch(conn, "operator_sessions", record.id))
        previous_selection, previous_leaves = self._selection, self._programme_current_leaves
        token = _SESSION_ROW_FORECAST.set((self, record.id, resolved))
        try:
            with _trace_memory_numeric_charges(self.header_budget) as trace:
                expected = await self._prepare_publication()
                # These are precisely the existing writer's repeated prior reads.
                if expected[1] is not None:
                    prior = read_accounting_checkpoint(self.workspace, header_budget=self.header_budget)
                    if prior is not None and prior.get("schema_version") == 2:
                        read_lifecycle_receipt(self.workspace, header_budget=self.header_budget)
                    read_lifecycle_receipt(self.workspace, header_budget=self.header_budget)
        finally:
            _SESSION_ROW_FORECAST.reset(token)
            self._selection, self._programme_current_leaves = previous_selection, previous_leaves
        entries = []
        for appearance, amount in trace:
            if appearance == ("body", "operator_sessions", record.id):
                amount = upper
            elif isinstance(appearance, tuple) and appearance[:2] == ("complete-headers", "operator_sessions"):
                # Forecast full header appearances at an upper bound for only
                # this resolved change; unrelated certified rows stay unchanged.
                amount = amount - original_upper + upper
            elif isinstance(appearance, tuple) and appearance[:2] == ("table-body", "operator_sessions") and record.id in appearance[2]:
                amount = amount - original_upper + upper
            entries.append((appearance, amount))
        if expected[1] is not None:
            from src.workspace.production import _publication_json_upper
            for label, payload in (("checkpoint", expected[1]), ("receipt", expected[2])):
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
                entries.extend([(("session-output", label), len(encoded)), (None, len(encoded) + 1)])
                for appearance in ("dict-copy", "json", "utf8", "readback-dict", "readback-json", "readback-utf8"):
                    entries.append((("composition-publication", label, appearance), _publication_json_upper(payload)))
        reservation = _SessionNumericReservation(self, binding, entries)
        self._session_reservation = reservation
        self._session_expected = tuple(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            for value in expected)
        self._session_instance = (record, tuple(changes.items()), resolved)

    def _validate_session_reservation(self):
        record, changes, resolved = self._session_instance
        if self.db.new or self.db.deleted or any(value is not record for value in self.db.dirty):
            raise ProductionWorkspaceReconciliationError("session_mutation_changed")
        from sqlalchemy.dialects.sqlite import dialect
        selected_dialect = dialect()
        actual = []
        for name, expected_value in resolved:
            column = record.__table__.columns[name]
            processor = column.type.dialect_impl(selected_dialect).bind_processor(selected_dialect)
            value = getattr(record, name)
            actual.append((name, processor(value) if processor is not None else value))
        if tuple(actual) != resolved:
            raise ProductionWorkspaceReconciliationError("session_mutation_changed")

    async def publish(self):
        from src.workspace.production import (write_accounting_checkpoint, write_lifecycle_receipt,
            _verify_composition_publication_bytes)
        if self.db.info.get("composition_sticky_failure") is not None:
            raise self.db.info["composition_sticky_failure"]
        reservation = self._session_reservation
        from src.db.engine import _original_discovery_exit_completion, _get_original_discovery_exit_completion
        original_exit = _original_discovery_exit_completion.get()
        discovery_reservation = None
        if original_exit is not None:
            owned, phase, job_id, _task = original_exit
            if (_get_original_discovery_exit_completion(owned, phase, job_id) is not original_exit
                    or owned.guard is not self or self._discovery_reservation is None):
                raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
            discovery_reservation = self._discovery_reservation
            discovery_reservation._checked()
            widths = discovery_reservation.publication_widths
            # The actual target and output objects are built by the unchanged
            # publisher below. Their closed numeric widths were reserved before
            # staging; pay the repeated construction appearances first.
            for label, key in (("target", "target_bytes"), ("checkpoint", "checkpoint_bytes"),
                    ("receipt", "receipt_bytes")):
                for appearance in ("construction", "canonical", "encoded"):
                    self.header_budget.debit(widths[key],
                        appearance=("discovery-publication-output", label, appearance))
        if reservation is not None:
            self._validate_session_reservation()
        self._writer_metadata_budget()
        await self.db.flush()
        if self.db.info.get("composition_sticky_failure") is not None:
            raise self.db.info["composition_sticky_failure"]
        if reservation is None:
            prepared = await self._prepare_publication()
            target, checkpoint, receipt = prepared
            if checkpoint is not None:
                if discovery_reservation is None:
                    write_accounting_checkpoint(self.workspace, checkpoint)
                    write_lifecycle_receipt(self.workspace, receipt, _accounting_lock_held=True)
                else:
                    write_accounting_checkpoint(self.workspace, checkpoint, header_budget=self.header_budget)
                    write_lifecycle_receipt(self.workspace, receipt, _accounting_lock_held=True,
                        header_budget=self.header_budget)
                    _verify_composition_publication_bytes(self.workspace, checkpoint, receipt,
                        header_budget=self.header_budget)
        else:
            with reservation.reads():
                prepared = await self._prepare_publication()
                if tuple(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
                        for value in prepared) != self._session_expected:
                    raise ProductionWorkspaceReconciliationError("session_publication_target_changed")
                target, checkpoint, receipt = prepared
                if checkpoint is not None:
                    for label, payload in (("checkpoint", checkpoint), ("receipt", receipt)):
                        self.header_budget.debit(len(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()),
                            appearance=("session-output", label))
                    write_accounting_checkpoint(self.workspace, checkpoint, header_budget=self.header_budget)
                    write_lifecycle_receipt(self.workspace, receipt, _accounting_lock_held=True, header_budget=self.header_budget)
                    _verify_composition_publication_bytes(self.workspace, checkpoint, receipt,
                        header_budget=self.header_budget)
            if any(reservation.entries.values()):
                raise ProductionWorkspaceReconciliationError("session_publication_appearance_missing")
            reservation.close()
        if original_exit is not None:
            owned, phase, job_id, _task = original_exit
            if _get_original_discovery_exit_completion(owned, phase, job_id) is not original_exit:
                raise ProductionWorkspaceReconciliationError("programme_original_writer_exit_unavailable")
            from src.memory.composition_headers import _metadata_size
            touched_upper = 2 + sum(4 + _metadata_size(table) + _metadata_size(key)
                + _metadata_size(before) for (table, key), before in self.touched.items())
            self.header_budget.debit(touched_upper,
                appearance=("discovery-publication-touched-copy", phase, job_id))
            self._discovery_publication_snapshot = (target, self._publication_members,
                self._programme_current_leaves, dict(self.touched))
            self._discovery_publication_exit = original_exit
        self.prepared_commit = True

    def close(self):
        from sqlalchemy import event
        for name, callback in self.listeners:
            event.remove(self.db.sync_session, name, callback)
        for connection, callback in self.connection_listeners:
            event.remove(connection, "before_cursor_execute", callback)
        if self._session_reservation is not None:
            self._session_reservation.close()
        self._session_instance = None
        self._session_expected = None
        self._retention_closed = True
        self._finish_programme_admission_selection()
        self._retention_writer_snapshot = None
        self._retention_read_budget = None
        self._writer_first_metadata_frame = None
        _HELD_COMPOSITION_WORKSPACE.reset(self.token)
        self.lock.__exit__(None, None, None)


async def prepare_composition_session(db, *, fresh=False, header_budget=None):
    """Acquire external ownership BEFORE any native SQL writer starts."""
    from sqlalchemy import text
    from config.settings import settings
    if db.in_transaction():
        raise ProductionWorkspaceReconciliationError("composition_lock_before_writer_required")
    exists = await db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_composition_states'"))
    populated = await db.scalar(text("SELECT 1 FROM runtime_composition_states LIMIT 1")) if exists else None
    if header_budget is not None:
        from src.memory.composition_headers import _metadata_cost
        header_budget.debit(_metadata_cost([exists, populated]), appearance=("composition-inventory-probes",))
    await db.rollback()
    if not populated and not fresh:
        return None
    lock = maintenance_accounting_lock(Path(settings.workspace_dir).resolve(), header_budget=header_budget)
    workspace = lock.__enter__()
    try:
        receipt = read_lifecycle_receipt(workspace, header_budget=header_budget) or {}
        expected = receipt.get("runtime_composition")
        programme_guard = None
        if type(expected) is dict and expected.get("schema_version") == 2:
            if header_budget is None:
                raise ProductionWorkspaceReconciliationError("programme_original_budget_required")
            _validate_programme_envelope(expected)
            programme_guard = CompositionSessionGuard(db, workspace, lock, expected, set(), header_budget=header_budget)
            db.info["composition_guard"] = programme_guard
        connection = await db.connection()
        def checked_snapshot(conn):
            if header_budget is None:
                return composition_closure(conn, verify_files=native_composition_files)
            from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection, snapshot_reads
            if not conn.connection.driver_connection.in_transaction:
                conn.exec_driver_sql("BEGIN")
            certificate = _certify_current_memory_snapshot_on_connection(conn, header_budget)
            if programme_guard is not None:
                return programme_guard._programme_snapshot(conn, certificate, expected)
            with snapshot_reads(certificate):
                return composition_closure(conn, verify_files=native_composition_files)
        base, members = await connection.run_sync(checked_snapshot)
        if base != expected or (fresh and (base is not None or expected is not None)):
            raise ProductionWorkspaceReconciliationError("composition_continuity_unavailable")
        from src.workspace.production import read_accounting_checkpoint
        checkpoint = read_accounting_checkpoint(workspace, header_budget=header_budget)
        if checkpoint and checkpoint.get("schema_version") == 2 and checkpoint.get("composition_target") != base:
            raise ProductionWorkspaceReconciliationError("composition_pending_checkpoint_requires_reconciliation")
        await db.rollback()
        read_guard = db.info.pop("composition_read_guard", None)
        if read_guard is not None:
            read_guard.close()
        guard = programme_guard or CompositionSessionGuard(db, workspace, lock, base, members, header_budget=header_budget)
        guard.base, guard.members = base, members
        if programme_guard is not None:
            guard._programme_base_leaves = dict(guard._programme_current_leaves)
        db.info["composition_guard"] = guard
        db.info["composition_base_witness"] = base
        guard.install()
        return guard
    except BaseException:
        if db.info.get("composition_guard") is locals().get("programme_guard") and programme_guard is not None:
            db.info.pop("composition_guard", None)
            _HELD_COMPOSITION_WORKSPACE.reset(programme_guard.token)
        lock.__exit__(None, None, None)
        raise


async def _prepare_composition_writer_session(db, *, owner, header_budget):
    """Private first retained writer; no admission or effect authority.

    Default reader preparation remains above. This route requires the original
    live numeric operation, preserves its frame, and retains the first writer.
    """
    from sqlalchemy import text
    from config.settings import settings
    from src.memory.header_bounds import HeaderReadBudget
    from src.memory.composition_headers import (
        _checked_current_snapshot_scope, _certify_current_memory_snapshot_on_connection,
        _metadata_cost, snapshot_reads)
    if (owner not in {"native_ingress", "composition_maintenance", "durable_jobs", "finite_service"}
            or type(header_budget) is not HeaderReadBudget
            or _checked_current_snapshot_scope(header_budget) is None
            or db.in_transaction() or db.in_nested_transaction()
            or db.new or db.dirty or db.deleted
            or db.info.get("composition_guard") is not None
            or db.info.get("native_writer_started")):
        raise ProductionWorkspaceReconciliationError("composition_writer_first_unavailable")
    # Preserve the original two bounded inventory probes and actual result debit.
    # They are not a zero-SQL precondition or a source/body permission.
    try:
        exists = await db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_composition_states'"))
        populated = await db.scalar(text("SELECT 1 FROM runtime_composition_states LIMIT 1")) if exists else None
        header_budget.debit(_metadata_cost([exists, populated]), appearance=("composition-inventory-probes",))
        await db.rollback()
    except BaseException:
        await db.rollback()
        raise
    if not populated:
        raise ProductionWorkspaceReconciliationError("composition_writer_first_unavailable")
    prior_owner_present = "composition_writer_owner" in db.info
    prior_owner = db.info.get("composition_writer_owner")
    prior_base_present = "composition_base_witness" in db.info
    prior_base = db.info.get("composition_base_witness")
    # Acquire the original lock before validating its deployment receipt, then
    # retain the paid return instead of reading the same private file again.
    lock = maintenance_accounting_lock(Path(settings.workspace_dir).resolve(),
        require_binding=False, header_budget=header_budget)
    workspace = lock.__enter__()
    guard = None
    try:
        receipt = assert_deployment_binding(workspace, header_budget=header_budget)
        expected = receipt.get("runtime_composition")
        if type(expected) is dict and expected.get("schema_version") == 2:
            _validate_programme_envelope(expected)
            guard = CompositionSessionGuard(db, workspace, lock, expected, set(), header_budget=header_budget)
            db.info["composition_guard"] = guard
        await db.execute(text("BEGIN IMMEDIATE"))
        connection = await db.connection()
        def checked_snapshot(conn):
            certificate = _certify_current_memory_snapshot_on_connection(
                conn, header_budget, _header_budget=header_budget)
            if guard is not None:
                return guard._programme_snapshot(conn, certificate, expected)
            with snapshot_reads(certificate):
                return composition_closure(conn, verify_files=native_composition_files)
        base, members = await connection.run_sync(checked_snapshot)
        if base != expected:
            raise ProductionWorkspaceReconciliationError("composition_continuity_unavailable")
        from src.workspace.production import read_accounting_checkpoint
        checkpoint = read_accounting_checkpoint(workspace, header_budget=header_budget)
        if checkpoint and checkpoint.get("schema_version") == 2 and checkpoint.get("composition_target") != base:
            raise ProductionWorkspaceReconciliationError("composition_pending_checkpoint_requires_reconciliation")
        read_guard = db.info.pop("composition_read_guard", None)
        if read_guard is not None:
            read_guard.close()
        guard = guard or CompositionSessionGuard(db, workspace, lock, base, members, header_budget=header_budget)
        guard.base, guard.members = base, members
        if type(expected) is dict and expected.get("schema_version") == 2:
            guard._programme_base_leaves = dict(guard._programme_current_leaves)
        db.info["composition_guard"] = guard
        db.info["composition_base_witness"] = base
        guard.install()
        db.info["composition_writer_owner"] = owner
        db.info["native_writer_started"] = True
        await connection.run_sync(
            lambda conn: guard._capture_native_writer_snapshot(conn, header_budget, owner))
        guard._issue_writer_first_metadata_mode(connection.sync_connection, header_budget)
        return guard
    except BaseException:
        try:
            await db.rollback()
        finally:
            db.info.pop("native_writer_started", None)
            if prior_owner_present:
                db.info["composition_writer_owner"] = prior_owner
            else:
                db.info.pop("composition_writer_owner", None)
            if prior_base_present:
                db.info["composition_base_witness"] = prior_base
            else:
                db.info.pop("composition_base_witness", None)
            if guard is not None:
                if db.info.get("composition_guard") is guard:
                    db.info.pop("composition_guard", None)
                guard.close()
            else:
                lock.__exit__(None, None, None)
        raise


def utc_period(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def period_state(account, rows, period):
    history = json.loads(account["settings_history_json"])
    authorized = datetime.fromisoformat(account["created_at"]).strftime("%Y-%m")
    reviewed = False
    periods = [authorized, *[row["period_id"] for row in rows]]
    for entry in history:
        if entry.get("kind") in {"period_initialized", "period_review"}:
            authorized = entry["period_id"]
            reviewed = entry.get("kind") == "period_review"
        if entry.get("kind") in {"period_initialized", "period_review", "period_observed"}:
            periods.append(entry["period_id"])
    high_water = max(periods)
    reason = ("accounting_clock_correction_required" if period < high_water and (period != authorized or not reviewed)
        else "accounting_period_review_required" if period != authorized else None)
    return {"authorized_period": authorized, "period_high_water": high_water, "reason_code": reason}


def unreviewed_overruns(account, rows):
    covered = set()
    for entry in json.loads(account["settings_history_json"]):
        if entry.get("kind") == "request_reserve_review":
            covered.update((row["operation_id"], row["sequence"], row["revision"]) for row in entry.get("operations", []))
    return [row for row in rows if row["state"] == "settled" and (row["actual_cost_microusd"] or 0) > row["bound_microusd"]
        and (row["operation_id"], row["sequence"], row["revision"]) not in covered]


def assert_deployment_binding(workspace, *, header_budget=None):
    directory = workspace.lifecycle_directory
    if (not os.environ.get(LIFECYCLE_PATH_ENV) and workspace.lifecycle_path is None
        or not directory.is_absolute() or directory.resolve() != directory or directory.is_relative_to(workspace.host_root)):
        raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
    receipt = read_lifecycle_receipt(workspace, header_budget=header_budget) or {}
    binding = receipt.get("deployment_binding")
    expected = (os.environ.get(BIND_IDENTITY_ENV) if workspace.host_root == Path(CANONICAL_CONTAINER_WORKSPACE)
        else workspace.identity_digest)
    field = "host_bind_identity" if workspace.host_root == Path(CANONICAL_CONTAINER_WORKSPACE) else "root_path_digest"
    if not isinstance(binding, dict) or not expected or binding.get(field) != expected:
        raise ProductionWorkspaceReconciliationError("accounting_deployment_binding_unavailable")
    return receipt


def ledger_record(values):
    record = dict(values)
    # Canonical writers own this unhashed private lookup projection. Raw
    # SQLite reads and archived checkpoints retain the existing financial wire.
    record.pop("group_lookup_key", None)
    for field in ("created_at", "updated_at", "deadline_at", "contact_started_at"):
        value = record.get(field)
        if isinstance(value, str):
            record[field] = datetime.fromisoformat(value).isoformat()
        elif isinstance(value, datetime):
            record[field] = value.isoformat()
    return record


def ledger_digest(account, rows):
    owner = ledger_record(account)
    owner.pop("ledger_digest", None)
    payload = {"account": owner, "operations": [ledger_record(row) for row in sorted(rows, key=lambda row: row["operation_id"])]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def witness(account):
    return {field: account[field] for field in ("deployment_id", "revision", "ledger_digest")}


@contextmanager
def maintenance_accounting_lock(root, *, require_binding=True, header_budget=None):
    workspace = root if isinstance(root, ProductionWorkspace) else ProductionWorkspace(host_root=root)
    if require_binding:
        assert_deployment_binding(workspace, header_budget=header_budget)
    directory = workspace.lifecycle_directory
    if directory.is_symlink() or not directory.is_dir():
        raise ProductionWorkspaceReconciliationError("accounting continuity unavailable")
    descriptor = os.open(directory / "accounting.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProductionWorkspaceReconciliationError("accounting continuity busy") from exc
        yield workspace
    finally:
        os.close(descriptor)


def configuration_digest(payload):
    normalized = dict(payload)
    normalized.pop("updated_at", None)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _assert_credential_free_configuration(payload):
    # This dependency-free path also reads archived JSON. Do not trust the
    # runtime validator to have validated a restored file before retaining it.
    from urllib.parse import urlsplit
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "").replace("_", "")
                if normalized in {"apikey", "authorization", "password", "secret", "token", "accesstoken", "refreshtoken"}:
                    raise ProductionWorkspaceReconciliationError("provider policy inline credential unavailable")
                if key in {"endpoint", "base_url", "api_base"} and isinstance(item, str) and urlsplit(item).username is not None:
                    raise ProductionWorkspaceReconciliationError("provider policy endpoint credential unavailable")
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    if not isinstance(payload, dict):
        raise ProductionWorkspaceReconciliationError("provider policy configuration unavailable")
    inspect(payload)
    near = payload.get("near_text")
    if near is not None and (not isinstance(near, dict) or near.get("api_base") != "https://cloud-api.near.ai/v1"):
        raise ProductionWorkspaceReconciliationError("provider policy endpoint unavailable")


def policy_continuity(workspace, payload):
    receipt = assert_deployment_binding(workspace)
    record = receipt.get("provider_policy")
    if (not isinstance(record, dict) or record.get("revision") != payload.get("egress_revision", 1)
        or record.get("configuration_digest") != configuration_digest(payload)):
        return False, record
    return True, record


def verify_restored_policy_reconciliation(*, root, archived, staged):
    """Permit only the owning, witnessed revocation of archived authority."""
    original, current = json.loads(archived), json.loads(staged)
    if not isinstance(original, dict) or not isinstance(current, dict):
        return False
    if (current.get("egress_revoked") is not True or current.get("egress_revocation_key") is not None
        or type(current.get("egress_revision")) is not int
        or current["egress_revision"] <= original.get("egress_revision", 1)):
        return False
    expected = {**original, "egress_revoked": True, "egress_revocation_key": None,
        "egress_revision": current["egress_revision"]}
    return current == expected and policy_continuity(ProductionWorkspace(host_root=root), current)[0]


def _write_configuration_file(path, payload):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class PolicyRevisionConflict(RuntimeError):
    pass


def _publish_policy_locked(workspace, payload, *, target_path, expected_revision=None):
    from src.workspace.production import write_lifecycle_receipt, _write_private_checkpoint
    _assert_credential_free_configuration(payload)
    receipt = read_lifecycle_receipt(workspace) or {}
    prior = receipt.get("provider_policy")
    if expected_revision is not None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise PolicyRevisionConflict("provider_policy_revision_changed")
        current = json.loads(target_path.read_text()) if target_path.is_file() else None
        empty_bootstrap = current is None and isinstance(prior, dict) and prior.get("revision") == 0 and expected_revision == 1
        if not empty_bootstrap and (not isinstance(current, dict) or current.get("egress_revision", 1) != expected_revision or not policy_continuity(workspace, current)[0]):
            raise PolicyRevisionConflict("provider_policy_revision_changed")
    revision = payload.get("egress_revision", 1)
    record = {"revision": revision, "configuration_digest": configuration_digest(payload),
        "state": "revoked" if payload.get("egress_revoked") else "active"}
    if (not isinstance(prior, dict) or type(revision) is not int
        or revision < prior["revision"] or revision == prior["revision"] and record != prior):
        raise ProductionWorkspaceReconciliationError("provider_policy_revision_changed")
    checkpoint = {"schema_version": 1, "base": prior, "target": record, "configuration": payload,
        "secret_values_included": False}
    _write_private_checkpoint(workspace.lifecycle_directory / "provider-policy-checkpoint.json", checkpoint)
    receipt["provider_policy"] = record
    write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
    _write_configuration_file(target_path, payload)
    return record


def publish_policy_configuration(root, payload, *, expected_revision=None):
    with maintenance_accounting_lock(root) as workspace:
        return _publish_policy_locked(workspace, payload, target_path=root / "model-fabric-settings.json", expected_revision=expected_revision)


def revoke_restored_policy(*, active, target):
    """Only the existing fenced lifecycle calls this staged publication."""
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace import canonical_workspace_registry
    _require_production_fence(canonical_workspace_registry(active.host_root))
    existing = read_lifecycle_receipt(active) or {}
    candidate = target / "model-fabric-settings.json"
    if not existing.get("provider_policy", {}).get("revision", 0) and candidate.is_file() and not candidate.is_symlink():
        contents = json.loads(candidate.read_text())
        if not isinstance(contents, dict) or not (contents.get("openrouter_setup") or contents.get("near_text")):
            return {"state": "uninitialized"}
    with maintenance_accounting_lock(active) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        path = target / "model-fabric-settings.json"
        if not path.is_file() or path.is_symlink():
            if receipt.get("provider_policy", {}).get("revision", 0):
                raise ProductionWorkspaceReconciliationError("provider policy restore configuration unavailable")
            return {"state": "uninitialized"}
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or not (payload.get("openrouter_setup") or payload.get("near_text")):
            if receipt.get("provider_policy", {}).get("revision", 0):
                raise ProductionWorkspaceReconciliationError("provider policy restore configuration unavailable")
            return {"state": "uninitialized"}
        payload.update(egress_revoked=True, egress_revocation_key=None,
            egress_revision=max(receipt.get("provider_policy", {}).get("revision", 0), payload.get("egress_revision", 1)) + 1)
        return _publish_policy_locked(workspace, payload, target_path=path)


def reconcile_policy_checkpoint(root):
    from src.workspace.production import _read_private_checkpoint
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace import canonical_workspace_registry
    _require_production_fence(canonical_workspace_registry(root))
    with maintenance_accounting_lock(root) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        checkpoint = _read_private_checkpoint(workspace.lifecycle_directory / "provider-policy-checkpoint.json")
        if checkpoint is None or checkpoint.get("target") != receipt.get("provider_policy"):
            raise ProductionWorkspaceReconciliationError("provider policy checkpoint unavailable")
        payload = checkpoint["configuration"]
        if configuration_digest(payload) != checkpoint["target"]["configuration_digest"]:
            raise ProductionWorkspaceReconciliationError("provider policy checkpoint digest mismatch")
        path = root / "model-fabric-settings.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        if configuration_digest(current) == checkpoint["target"]["configuration_digest"]:
            return {"status": "already_reconciled", "revision": checkpoint["target"]["revision"]}
        # Completing an interrupted active write must never activate a grant.
        if not payload.get("egress_revoked"):
            payload = {**payload, "egress_revoked": True, "egress_revocation_key": None,
                "egress_revision": checkpoint["target"]["revision"] + 1}
        result = _publish_policy_locked(workspace, payload, target_path=path)
        return {"status": "reconciled_revoked", "revision": result["revision"], "job_authority_changed": False}
