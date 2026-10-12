"""Dependency-free encoding shared by the runtime and managed lifecycle CLI."""

from contextlib import contextmanager, asynccontextmanager
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
_MEMORY_RETENTIONS = WeakValueDictionary()
_MEMORY_RETENTION_PROOFS = ContextVar("original_memory_retention_proofs", default=None)
_MEMORY_INTEGER_SLOTS = WeakValueDictionary()

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


def _native_memory_key_set_width(value):
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if type(value) is not dict or any(type(name) is not str for name in value):
        raise HeaderBoundsError("memory_reference_payload_invalid")
    return 2 + max(0, len(value) - 1) + sum(_native_memory_ascii_string_width(name) for name in value)


def preflight_native_memory_reference_payload(payload, *, current=False, header_budget=None):
    """Closed byte-shape parser only; no source, row, MAC or effect authority.

    Actual owner publication and current-row validation remain mandatory and
    unavailable until the complete source-issued contract is installed.
    """
    import re
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, MAX_ROWS, MEMORY_DESCRIPTORS
    from src.memory.composition_headers import current_budget
    from src.memory.header_bounds import _native_memory_utf8_width, _native_memory_ascii_string_width
    payer = header_budget if header_budget is not None else current_budget()
    def pay(stage, upper):
        if payer is not None:
            payer.debit(upper, appearance=("memory-reference-payload", "current" if current else "original", stage))
    def denied():
        raise HeaderBoundsError("memory_reference_payload_invalid")
    def sha(value):
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            denied()
    def key(value):
        try:
            if type(value) is str:
                pay("key-utf8", _native_memory_utf8_width(value))
            valid = type(value) is str and bool(value) and len(value.encode("utf-8")) <= 512
        except UnicodeEncodeError:
            valid = False
        if not valid:
            denied()
    def bounded_integer(value, upper):
        if type(value) is not int or not 0 <= value <= upper:
            denied()
    def reference(value):
        if type(value) is dict:
            pay("reference-field-set", _native_memory_key_set_width(value))
        pay("reference-table-set", 2 + sum(_native_memory_ascii_string_width(name) + 1
            for name in RETAINED_FIELDS) + sum(_native_memory_ascii_string_width(name) + 1
            for name in MEMORY_DESCRIPTORS))
        if (type(value) is not dict or set(value) != {"table", "key"}
                or type(value["table"]) is not str
                or value["table"] not in {*RETAINED_FIELDS, *MEMORY_DESCRIPTORS}):
            denied()
        key(value["key"])
        pay("reference-tuple", 3 + _native_memory_ascii_string_width(value["table"])
            + _native_memory_ascii_string_width(value["key"]))
        return value["table"], value["key"]
    def reference_list(values):
        if type(values) is not list or len(values) > MAX_ROWS:
            denied()
        upper = _native_memory_closed_value_width(values)
        for stage in ("reference-list", "reference-set", "reference-sort"):
            pay(stage, upper)
        result = [reference(item) for item in values]
        if result != sorted(set(result)):
            denied()
        return result
    fields = _MEMORY_CURRENT_FIELDS if current else _MEMORY_ORIGINAL_FIELDS
    if type(payload) is dict:
        pay("payload-field-set", _native_memory_key_set_width(payload))
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
        pay("absence-list", _native_memory_closed_value_width(payload["absences"]))
        absent = []
        for item in payload["absences"]:
            if type(item) is dict:
                pay("absence-field-set", _native_memory_key_set_width(item))
            if type(item) is not dict or set(item) != {"ref", "reason", "original_binding_digest"}:
                denied()
            ref = reference(item["ref"])
            if (type(item["reason"]) is not str
                    or (ref[0], item["reason"]) not in {("goals", "goal_deleted"), ("memories", "memory_deleted")}
                    or ref not in refs):
                denied()
            sha(item["original_binding_digest"])
            absent.append(ref)
        union_upper = _native_memory_closed_value_width(refs) + _native_memory_closed_value_width(events)
        for stage in ("absence-set", "absence-sort", "reference-union-tuple", "reference-union-set"):
            pay(stage, union_upper)
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
            pay("row-reference-list", _native_memory_closed_value_width(payload["rows"]))
            row_refs = []
            for row in payload["rows"]:
                if type(row) is dict:
                    pay("row-field-set", _native_memory_key_set_width(row))
                if type(row) is not dict or set(row) != {"ref", "tuple_digest", "encoded_bytes"}:
                    denied()
                row_refs.append(reference(row["ref"]))
                sha(row["tuple_digest"])
                bounded_integer(row["encoded_bytes"], MAX_BYTES)
            for stage in ("row-reference-union", "row-reference-set", "row-reference-sort"):
                pay(stage, union_upper)
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
        if type(audit) is dict:
            pay("audit-field-set", _native_memory_key_set_width(audit))
        if type(audit) is not dict or set(audit) != {"ref", "tuple_digest"}:
            denied()
        audit_ref = reference(audit["ref"])
        if audit_ref[0] != "audit_events" or audit_ref not in refs:
            denied()
        sha(audit["tuple_digest"])
        try:
            for stage in ("deadline-parse", "deadline-zone", "deadline-isoformat"):
                pay(stage, _native_memory_closed_value_width(payload["original_deadline"]))
            deadline = datetime.fromisoformat(payload["original_deadline"])
            if deadline.tzinfo is None or deadline.astimezone(timezone.utc).isoformat() != payload["original_deadline"]:
                denied()
        except (TypeError, ValueError):
            denied()
    upper = _native_memory_closed_value_width(payload)
    pay("canonical", upper)
    pay("encoded", upper)
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


def preflight_native_memory_reference_journal(raw, *, header_budget=None):
    """Bounded closed checkpoint parser, never a mint/copy/adoption permission.

    Invalid Original is reported to the actual reduction owner; it cannot be
    replaced with an invented Original. This helper only parses valid shapes.
    The original owner must first certify this raw column's whole-row header.
    """
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    from src.memory.header_bounds import _native_memory_utf8_width
    from src.memory.composition_headers import current_budget
    from src.workflows.job_runtime import _digest
    payer = header_budget if header_budget is not None else current_budget()
    if payer is not None:
        payer.debit(6 * _native_memory_utf8_width(raw) + 2,
            appearance=("memory-reference-journal", "decode"))
    records = strict_json_loads(raw)
    if type(records) is not list:
        raise HeaderBoundsError("memory_reference_journal_invalid")
    if payer is not None:
        payer.debit(2, appearance=("memory-reference-journal", "found-map"))
    found = {}
    for record in records:
        if type(record) is not dict:
            raise HeaderBoundsError("memory_reference_journal_invalid")
        identifier = record.get("checkpoint_id")
        if identifier not in (MEMORY_ORIGINAL_CHECKPOINT, MEMORY_CURRENT_CHECKPOINT):
            continue
        if payer is not None:
            payer.debit(_native_memory_key_set_width(record),
                appearance=("memory-reference-journal", "record-field-set", identifier))
        if (identifier in found or set(record) != {"checkpoint_id", "state_digest", "safe", "payload"}
                or record["safe"] is not True):
            raise HeaderBoundsError("memory_reference_journal_invalid")
        preflight_native_memory_reference_payload(record["payload"],
            current=identifier == MEMORY_CURRENT_CHECKPOINT, header_budget=payer)
        if payer is not None:
            upper = _native_memory_closed_value_width(record["payload"])
            for stage in ("digest-canonical", "digest-encoded"):
                payer.debit(upper, appearance=("memory-reference-journal", stage, identifier))
            payer.debit(66, appearance=("memory-reference-journal", "digest-result", identifier))
        if record["state_digest"] != _digest(record["payload"]):
            raise HeaderBoundsError("memory_reference_journal_invalid")
        found[identifier] = record
    if payer is not None:
        payer.debit(4, appearance=("memory-reference-journal", "return"))
    return found.get(MEMORY_ORIGINAL_CHECKPOINT), found.get(MEMORY_CURRENT_CHECKPOINT)


def _native_memory_lexical_token_upper(raw, position):
    """Size an already strictly validated lexical token without decoding it."""
    from src.memory.header_bounds import HeaderBoundsError
    start = position
    depth = 0
    quoted = False
    escaped = False
    container = raw[position] in "[{"
    while position < len(raw):
        char = raw[position]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
                if not container:
                    position += 1
                    break
        elif char == '"':
            quoted = True
        elif container:
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth == 0:
                    position += 1
                    break
        elif char in ",]} \t\r\n":
            break
        position += 1
    if position <= start or quoted or depth:
        raise HeaderBoundsError("memory_reference_journal_invalid")
    utf8 = sum(1 if ord(raw[index]) < 128 else 2 if ord(raw[index]) < 2048
        else 3 if ord(raw[index]) <= 65535 else 4 for index in range(start, position))
    # For a valid ASCII JSON string the existing escapes already bound its
    # canonical representation. DEL/nonASCII requires the original 6x upper.
    if raw[start] == '"' and all(ord(raw[index]) < 127 for index in range(start, position)):
        return position - start
    return 6 * utf8 + 2


def _splice_native_memory_current_checkpoint(raw, replacement, *, header_budget=None):
    """Bounded lexical replacement only; never publication or owner authority.

    Preserve every character outside the old Current record, including other
    checkpoint escapes and whitespace. The actual owner must separately bind
    the original journal, transaction and protected revision in its SQL CAS.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, strict_json_loads
    from src.workflows.job_runtime import _canonical, _digest
    from src.memory.composition_headers import current_budget
    from src.memory.header_bounds import _native_memory_utf8_width
    payer = header_budget if header_budget is not None else current_budget()
    raw_upper = 6 * _native_memory_utf8_width(raw) + 2 if payer is not None else 0
    def pay(stage, upper):
        if payer is not None:
            payer.debit(upper, appearance=("memory-current-splice", stage))
    pay("journal-decode", raw_upper)
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
    preflight_native_memory_reference_payload(replacement["payload"], current=True, header_budget=payer)
    replacement_upper = _native_memory_closed_value_width(replacement) if payer is not None else 0
    payload_upper = _native_memory_closed_value_width(replacement["payload"]) if payer is not None else 0
    for stage in ("payload-canonical", "payload-encoded"):
        pay(stage, payload_upper)
    pay("payload-digest", 66)
    if replacement["state_digest"] != _digest(replacement["payload"]):
        raise HeaderBoundsError("memory_reference_journal_invalid")
    # The exact wrapper parser checks shape, safe=True and the existing runtime
    # digest codec. This does not authorize the Current represented by it.
    try:
        pay("record-canonical", replacement_upper)
        encoded = _canonical(replacement)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise HeaderBoundsError("memory_reference_journal_invalid") from None
    pay("wrapper-copy", replacement_upper + 2)
    preflight_native_memory_reference_journal("[" + encoded + "]", header_budget=payer)
    pay("span-list", raw_upper)
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
        # The selected raw journal is bounded before each original decoder
        # appearance, including the repeated lexical pass over its records.
        pay("record-decode", _native_memory_lexical_token_upper(raw, position) if payer is not None else 0)
        _, position = decoder.raw_decode(raw, position)
        spans.append((start, position))
        while raw[position] in whitespace:
            position += 1
        if raw[position] == ",":
            position += 1
    pay("matches", raw_upper)
    matches = [index for index, record in enumerate(records)
               if record.get("checkpoint_id") == MEMORY_CURRENT_CHECKPOINT]
    if matches:
        start, end = spans[matches[0]]
        pay("prefix-copy", raw_upper)
        pay("suffix-copy", raw_upper)
        pay("join-copy", raw_upper + replacement_upper)
        result = raw[:start] + encoded + raw[end:]
    else:
        # Insert after the last record, preserving its trailing whitespace, or
        # directly after '[' for an empty array. Only one delimiter is added.
        insert_at = spans[-1][1] if spans else position
        pay("prefix-copy", raw_upper)
        pay("suffix-copy", raw_upper)
        pay("join-copy", raw_upper + replacement_upper + 1)
        result = raw[:insert_at] + ("," if spans else "") + encoded + raw[insert_at:]
    pay("size-utf8", raw_upper + replacement_upper + 1)
    if len(result.encode("utf-8")) > MAX_BYTES:
        raise HeaderBoundsError("header_json_bound")
    return result


def _native_memory_planned_sql_row(instance, changes=None, *, transient=False, _capacity_slot=None):
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
    if _capacity_slot is not None and type(_capacity_slot) is not _NativeMemoryFutureIntegerSlot:
        raise HeaderBoundsError("memory_future_integer_slot_changed")
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
            capacity_key = (_capacity_slot is not None and name == descriptor.key
                and descriptor.table == "work_board_events" and kind == "integer")
            if capacity_key:
                _capacity_slot._checked()
                if (_capacity_slot.instance is not instance or _capacity_slot.descriptor is not descriptor
                        or _capacity_slot.bound_key is not None or not transient
                        or not column.primary_key):
                    raise HeaderBoundsError("memory_future_integer_slot_changed")
            elif not nullable:
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


def _native_memory_own_journal_projection(raw, run_identity, *, header_budget=None):
    """Replace exactly the adopted three lexical values in a temporary copy."""
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    from src.memory.composition_headers import current_budget
    from src.memory.header_bounds import _native_memory_utf8_width
    payer = header_budget if header_budget is not None else current_budget()
    upper = 6 * _native_memory_utf8_width(raw) + 2 if payer is not None else 0
    def pay(stage, amount=upper):
        if payer is not None:
            payer.debit(amount, appearance=("memory-own-journal", run_identity, stage))
    pay("decode")
    records = strict_json_loads(raw)
    _, current = preflight_native_memory_reference_journal(raw, header_budget=payer)
    if current is None or current["payload"]["state"] != "validated":
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    record_index = next(index for index, record in enumerate(records)
        if record.get("checkpoint_id") == MEMORY_CURRENT_CHECKPOINT)
    pay("own-list")
    own = [index for index, row in enumerate(current["payload"]["rows"])
        if row["ref"] == {"table": "workflow_run_states", "key": run_identity}]
    if len(own) != 1:
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    pay("replacement-map")
    replacements = {
        (record_index, "state_digest"): '"' + "0" * 64 + '"',
        (record_index, "payload", "rows", own[0], "tuple_digest"): '"' + "0" * 64 + '"',
        (record_index, "payload", "rows", own[0], "encoded_bytes"): "0",
    }
    decoder = json.JSONDecoder()
    pay("span-list")
    spans = []
    def whitespace(position):
        while position < len(raw) and raw[position] in " \t\r\n":
            position += 1
        return position
    def visit(position, path):
        position = whitespace(position)
        start = position
        if path in replacements:
            pay("token-decode", _native_memory_lexical_token_upper(raw, position) if payer is not None else 0)
            _, end = decoder.raw_decode(raw, position)
            spans.append((start, end, replacements[path]))
            return end
        if raw[position] == "{":
            position = whitespace(position + 1)
            while raw[position] != "}":
                pay("key-decode", _native_memory_lexical_token_upper(raw, position) if payer is not None else 0)
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
        pay("token-decode", _native_memory_lexical_token_upper(raw, position) if payer is not None else 0)
        return decoder.raw_decode(raw, position)[1]
    visit(0, ())
    if len(spans) != 3:
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    pay("span-sort")
    for start, end, replacement in sorted(spans, reverse=True):
        for stage in ("prefix-copy", "suffix-copy", "join-copy"):
            pay(stage)
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


def _native_memory_closed_value_width(value):
    """Encoded-unit arithmetic for already selected closed Memory values.

    No serializer, UTF8 copy, digest, row construction or permission is issued
    here. Callers must establish their original Source and body certificate.
    """
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is str:
        return _native_memory_ascii_string_width(value)
    if type(value) is int and -(2**63) <= value < 2**63:
        # Decimal width without constructing the serializer's temporary text.
        number = abs(value)
        width = 1 + (value < 0)
        while number >= 10:
            number //= 10
            width += 1
        return width
    if type(value) is float and math.isfinite(value):
        # Preserve the original finite-real encoded-unit upper.
        return 64
    if type(value) is list or type(value) is tuple:
        return 2 + max(0, len(value) - 1) + sum(
            _native_memory_closed_value_width(item) for item in value)
    if type(value) is dict and all(type(name) is str for name in value):
        return 2 + max(0, len(value) - 1) + sum(
            _native_memory_ascii_string_width(name) + 1 + _native_memory_closed_value_width(item)
            for name, item in value.items())
    raise HeaderBoundsError("memory_numeric_shape_unavailable")


def _native_memory_current_tuple_upper(raw_row, journal_utf8_upper):
    """Preserve the original temporary-tuple fee without allocating to size."""
    from src.memory.header_bounds import WRS_BY_RUN, _native_memory_tuple_skeleton_width, _native_memory_utf8_width
    return _native_memory_tuple_skeleton_width(WRS_BY_RUN, raw_row["run_identity"]) + sum(
        6 * (journal_utf8_upper if name == "checkpoint_receipts_json" else _native_memory_utf8_width(value)) + 2
        if type(value) is str else 32 if value is not None else 0
        for name, value in raw_row.items())


def _native_memory_current_allocation_entries(identity, *, row_upper, payload_upper,
        rows_upper, record_upper, stage):
    """Source-owned Current allocation grammar; no values or permissions.

    This is one setup/build/finalization invocation, not a complete effect
    forecast. The effect owner must separately include the original payload
    preflight, lexical splice and journal normalization owners' appearances.
    """
    from src.memory.header_bounds import HeaderBoundsError
    if (type(identity) is not str or stage not in {"setup", "build", "finish"}
            or any(type(value) is not int or value < 0 for value in (
                row_upper, payload_upper, rows_upper, record_upper))):
        raise HeaderBoundsError("memory_current_numeric_shape_unavailable")
    amounts = {
        "setup": (("row-copy", row_upper), ("payload-copy", payload_upper),
            ("own-row-list", rows_upper), ("own-row-update", rows_upper),
            ("rows-normalized-copy", rows_upper), ("rows-canonical", rows_upper),
            ("rows-encoded", rows_upper), ("rows-digest", 66)),
        "build": (("count-update", 20), ("payload-canonical", payload_upper),
            ("payload-encoded", payload_upper), ("payload-digest", 66),
            ("record", record_upper), ("candidate-copy", row_upper),
            ("raw-codec-fields", row_upper), ("raw-codec-canonical", row_upper),
            ("raw-codec-encoded", row_upper), ("return", row_upper + record_upper)),
        "finish": (("projection-copy", row_upper), ("projection-codec-fields", row_upper),
            ("projection-codec-canonical", row_upper), ("projection-codec-encoded", row_upper),
            ("projection-digest", 66), ("digest-update", 66),
            ("return", row_upper + record_upper)),
    }[stage]
    return tuple((("memory-current-allocation", identity, name), amount)
        for name, amount in amounts)


def _native_memory_reference_record_upper(payload_upper, *, current):
    from src.memory.header_bounds import _native_memory_object_width, _native_memory_ascii_string_width
    return _native_memory_object_width({
        "checkpoint_id": _native_memory_ascii_string_width(
            MEMORY_CURRENT_CHECKPOINT if current else MEMORY_ORIGINAL_CHECKPOINT),
        "safe": 4, "state_digest": 66, "payload": payload_upper})


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
    row_width = _native_memory_closed_value_width(raw_row)
    payload_width = _native_memory_closed_value_width(payload)
    rows_width = _native_memory_closed_value_width(payload["rows"])
    identity = raw_row["run_identity"]
    # The existing full raw-row upper also covers every closed dict/list
    # envelope used by its codec. These are separate paid appearances.
    def pay(stage, *, candidate_width=row_width,
            record_width=_native_memory_reference_record_upper(payload_width, current=True)):
        for appearance, amount in _native_memory_current_allocation_entries(identity,
                row_upper=candidate_width, payload_upper=_native_memory_closed_value_width(payload),
                rows_upper=rows_width, record_upper=record_width,
                stage=stage):
            header_budget.debit(amount, appearance=appearance)
    pay("setup")
    row, payload = dict(raw_row), deepcopy(payload)
    own = [item for item in payload["rows"]
           if item["ref"] == {"table": "workflow_run_states", "key": identity}]
    if len(own) != 1:
        raise HeaderBoundsError("memory_current_self_projection_unavailable")
    own = own[0]
    own.update(tuple_digest="0" * 64, encoded_bytes=0)
    payload["rows_digest"] = _native_memory_current_rows_digest(payload["rows"], identity)
    original_journal = row["checkpoint_receipts_json"]
    def build():
        header_budget.debit(20, appearance=("memory-current-allocation", identity, "count-update"))
        payload["encoded_bytes"] = sum(item["encoded_bytes"] for item in payload["rows"])
        preflight_native_memory_reference_payload(payload, current=True, header_budget=header_budget)
        payload_upper = _native_memory_closed_value_width(payload)
        record_upper = _native_memory_reference_record_upper(payload_upper, current=True)
        for name, amount in (("payload-canonical", payload_upper), ("payload-encoded", payload_upper),
                ("payload-digest", 66), ("record", record_upper)):
            header_budget.debit(amount, appearance=("memory-current-allocation", identity, name))
        record = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "safe": True,
                  "state_digest": _digest(payload), "payload": payload}
        journal = _splice_native_memory_current_checkpoint(original_journal, record, header_budget=header_budget)
        # Count all temporary raw appearances before encoding the full tuple.
        # Six times UTF8 is a conservative bound for canonical ASCII escaping.
        candidate_width = (row_width - _native_memory_closed_value_width(original_journal)
            + _native_memory_closed_value_width(journal))
        header_budget.debit(candidate_width,
            appearance=("memory-current-allocation", identity, "candidate-copy"))
        from src.memory.header_bounds import _native_memory_utf8_width
        upper = _native_memory_current_tuple_upper(row, _native_memory_utf8_width(journal))
        for name in ("raw-codec-fields", "raw-codec-canonical", "raw-codec-encoded"):
            header_budget.debit(upper, appearance=("memory-current-allocation", identity, name))
        candidate = dict(row, checkpoint_receipts_json=journal)
        header_budget.debit(upper)
        encoded = _native_memory_row_bytes(WRS_BY_RUN, identity, candidate)
        if len(encoded) > MAX_BYTES:
            raise HeaderBoundsError("canonical_bound_not_certified")
        header_budget.debit(candidate_width + record_upper,
            appearance=("memory-current-allocation", identity, "return"))
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
    from src.memory.header_bounds import _native_memory_utf8_width
    pay("finish", candidate_width=_native_memory_current_tuple_upper(candidate,
        _native_memory_utf8_width(candidate["checkpoint_receipts_json"])),
        record_width=_native_memory_closed_value_width(record))
    projected = dict(candidate)
    # The normalized temporary tuple is also a charged byte appearance.
    header_budget.debit(len(encoded))
    projected["checkpoint_receipts_json"] = _native_memory_own_journal_projection(
        candidate["checkpoint_receipts_json"], identity, header_budget=header_budget)
    own["tuple_digest"] = hashlib.sha256(_native_memory_row_bytes(WRS_BY_RUN, identity, projected)).hexdigest()
    candidate, record, final_bytes = build()
    if len(final_bytes) != measured or record["payload"]["encoded_bytes"] != sum(
            item["encoded_bytes"] for item in record["payload"]["rows"]):
        raise HeaderBoundsError("memory_current_count_not_convergent")
    return candidate, record


def _native_memory_reference_preflight_numeric_entries(operation, prepared, *, certificate,
        retention_widths, current):
    """One closed own Original/validated Current payload parser appearance.

    References remain genuine Source addresses or their private numeric slot.
    The vector conveys capacity only; no payload, Ref or certificate is made.
    """
    from src.memory.header_bounds import (MAX_BYTES, HeaderBoundsError, MEMORY_DESCRIPTORS,
        _native_memory_ascii_string_width as width, _native_memory_utf8_width as utf8)
    from src.runtime_plugins import memory_producer as source_owner
    if (type(operation) is not source_owner._MemoryOwnerSourceOperation
            or source_owner._MEMORY_OPERATIONS.get(id(operation)) is not operation):
        raise HeaderBoundsError("memory_current_numeric_owner_unavailable")
    guard = operation.db.info.get("composition_guard")
    facts = guard._native_memory_source_capacity_facts[3]
    _guard, rows, own, audit, new_addresses, value = _native_memory_generic_future_bound_inputs(
        operation, prepared, certificate, facts, retention_widths)
    if type(current) is not bool:
        raise HeaderBoundsError("memory_current_numeric_shape_unavailable")
    w, result = retention_widths, []
    prefix = ("memory-reference-payload", "current" if current else "original")
    def pay(stage, upper):
        result.append(((*prefix, stage), upper))
    def field_set(names):
        return 2 + max(0, len(names) - 1) + sum(width(name) for name in names)
    def pair(address):
        table, key = address
        return 3 + width(table) + (22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key))
    def pairs(addresses):
        return 2 + max(0, len(addresses) - 1) + sum(pair(address) for address in addresses)
    def reference(address):
        table, key = address
        pay("reference-field-set", field_set(("table", "key")))
        pay("reference-table-set", 2 + sum(width(name) + 1 for name in RETAINED_FIELDS)
            + sum(width(name) + 1 for name in MEMORY_DESCRIPTORS))
        pay("key-utf8", 20 if type(key) is _NativeMemoryFutureIntegerSlot else utf8(key))
        pay("reference-tuple", pair(address))
    refs = tuple(rows)
    events = tuple(address for address in new_addresses if address[0] in {"audit_events", "work_board_events"})
    pay("payload-field-set", field_set(_MEMORY_CURRENT_FIELDS if current else _MEMORY_ORIGINAL_FIELDS))
    for stage in ("reference-list", "reference-set", "reference-sort"):
        pay(stage, w["refs_upper"])
    for address in refs:
        reference(address)
    if current:
        event_upper = 2 + max(0, len(events) - 1) + sum(
            field_set(("table", "key")) + 2 + width(table)
            + (22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)) for table, key in events)
        for stage in ("reference-list", "reference-set", "reference-sort"):
            pay(stage, event_upper)
        for address in events:
            reference(address)
        pay("absence-list", 2)
        union_upper = pairs(refs) + pairs(events)
        for stage in ("absence-set", "absence-sort", "reference-union-tuple", "reference-union-set"):
            pay(stage, union_upper)
        pay("row-reference-list", w["rows_upper"])
        for address in refs:
            pay("row-field-set", field_set(("ref", "tuple_digest", "encoded_bytes")))
            reference(address)
        for stage in ("row-reference-union", "row-reference-set", "row-reference-sort"):
            pay(stage, union_upper)
    else:
        for scalar in (own[1], operation.scope.witness["claim_ref"],
                operation.source.operator.principal.principal_id, operation.source.operator.session_id):
            pay("key-utf8", utf8(scalar))
        pay("audit-field-set", field_set(("ref", "tuple_digest")))
        reference(audit)
        candidate_json = operation.source.admission.candidate_json
        marker = '"original_deadline":'
        position = candidate_json.find(marker)
        if position < 0:
            raise HeaderBoundsError("memory_current_deadline_numeric_unavailable")
        deadline_upper = _native_memory_lexical_token_upper(candidate_json, position + len(marker))
        for stage in ("deadline-parse", "deadline-zone", "deadline-isoformat"):
            pay(stage, deadline_upper)
    for stage in ("canonical", "encoded"):
        pay(stage, w["current_upper" if current else "original_upper"])
    maxima = {}
    for appearance, upper in result:
        maxima[appearance] = max(upper, maxima.get(appearance, 0))
    result = tuple((appearance, maxima[appearance]) for appearance, _upper in result)
    if any(type(upper) is not int or not 0 <= upper <= MAX_BYTES for _appearance, upper in result):
        raise HeaderBoundsError("canonical_bound_not_certified")
    return result


def _native_memory_original_preflight_construction_numeric_upper(operation, prepared, *,
        certificate, facts, retention_widths):
    """Constructor capacity for ONE original capture payload preflight vector.

    Excludes body/parser costs in that returned vector, Current normalization,
    common/Universe and caller-retained lineage. No vector/body/query is made.
    """
    from src.runtime_plugins import memory_producer as source_owner
    from src.memory.header_bounds import HeaderBoundsError, MAX_ROWS, MEMORY_DESCRIPTORS
    from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
    source_owner._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard = operation.db.info.get("composition_guard")
    source = operation.source
    projection = getattr(guard, "_native_memory_source_projection", None)
    if (source_owner._MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or type(certificate) is not CompositionHeaderCertificate
            or certificate.seal is not _SEAL or certificate.issued_id != id(certificate)
            or certificate.driver is not operation.driver
            or certificate.metadata_budget is not certificate.budget
            or guard.header_budget is not certificate.budget
            or source_owner._native_memory_source_numeric_frame() is not certificate.budget
            or type(projection) is not tuple or len(projection) != 3
            or projection[0] is not operation):
        raise HeaderBoundsError("memory_original_preflight_construction_owner_changed")
    row_count = len(facts["row_uppers"])
    if not 1 <= row_count <= MAX_ROWS or retention_widths["row_count"] != row_count:
        raise HeaderBoundsError("memory_original_preflight_construction_shape_unavailable")
    event_count = 0
    for descriptor, identity, row in projection[1]:
        if descriptor.table in ("audit_events", "work_board_events"):
            event_count += 1
    # The actual Source candidate has already been parsed and authenticated.
    # The existing builder scans this exact borrowed JSON for its deadline;
    # require its marker here without constructing any substring/parsed body.
    candidate = source.admission.candidate_json
    if type(candidate) is not str or candidate.find('"original_deadline":') < 0:
        raise HeaderBoundsError("memory_original_preflight_deadline_unavailable")
    # Output O: field-set1 + ref-list/set/sort3 + reference4R + four owner-key
    # UTF8 appearances + audit field-set1/reference4 + deadline3 + canonical2.
    output_count = 18 + 4 * row_count
    # Original generic owner creates its one selected/projection row map plus
    # sets/new-addresses/return closure. Immutable references use capacity4 and
    # each numeric scalar has original signed-int64 width20.
    owner_storage = 1024 + 128 * row_count
    prefix_storage = 2 + 2 * 4 + 1
    reference_arrays = (2 + 5 * row_count) + (2 + 5 * event_count)
    # pay(): new three-ref label14 + pair37 + list slot5. Maxima dict entry27;
    # final normalized pair27 + tuple slot5. Include each distinct structure.
    vector_storage = 6 + output_count * (14 + 37 + 5 + 27 + 27 + 5)
    # Fixed four-scalar source-identity argument tuple, payload prefix and
    # reference/audit field name containers are separate from the vector.
    temporary_storage = (2 + 4 * 4) + (2 + 2 * 4) + (2 + 2 * 4)
    # Original field_set traverses each literal field; reference() runs R+1
    # times and constructs numerical width generators over the exact table
    # roster, never over guessed row-byte capacities. Eight local functions
    # plus fixed iterators/deadline scans/maxima traversal fit32 control slots.
    reference_controls = (row_count + 1) * (8 + len(RETAINED_FIELDS) + len(MEMORY_DESCRIPTORS))
    controls = 32 * (32 + 4 * row_count + len(_MEMORY_ORIGINAL_FIELDS) + reference_controls)
    return owner_storage + prefix_storage + reference_arrays + vector_storage + temporary_storage + controls


def _native_memory_original_preflight_numeric_entries(operation, prepared, *, certificate, retention_widths):
    """ONE original capture payload preflight, separately composed by its owner."""
    return _native_memory_reference_preflight_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=retention_widths, current=False)


def _native_memory_current_construction_numeric_entries(operation, prepared, *, certificate, retention_widths):
    """ONE own Current construction, including all 33 guarded build liabilities.

    Supplied-current-certificate numeric facts only. This excludes certificate
    acquisition, capture, publication and subsequent cold body readers.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width as width
    from src.runtime_plugins import memory_producer as source_owner
    if (type(operation) is not source_owner._MemoryOwnerSourceOperation
            or source_owner._MEMORY_OPERATIONS.get(id(operation)) is not operation):
        raise HeaderBoundsError("memory_current_numeric_owner_unavailable")
    guard = operation.db.info.get("composition_guard")
    facts = guard._native_memory_source_capacity_facts[3]
    _guard, rows, own, audit, new_addresses, value = _native_memory_generic_future_bound_inputs(
        operation, prepared, certificate, facts, retention_widths)
    w, identity = retention_widths, own[1]
    current = _native_memory_reference_preflight_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=w, current=True)
    original = _native_memory_original_preflight_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=w)
    raw = value(own, "checkpoint_receipts_json")
    if type(raw) is not str:
        raise HeaderBoundsError("memory_current_prior_journal_unavailable")
    # Structural lexical arithmetic on an already-paid strict original column.
    # Retain numerical token/record widths; no decoded record or future body.
    lexical, prior_records = [], []
    position, depth = 0, 0
    while position < len(raw):
        char = raw[position]
        if char in " \t\r\n,:":
            position += 1
            continue
        if char in "[{":
            if char == "{" and depth == 1:
                prior_records.append(_native_memory_lexical_token_upper(raw, position))
            depth += 1
            position += 1
            continue
        if char in "]}":
            depth -= 1
            position += 1
            continue
        start = position
        if char == '"':
            position += 1
            while position < len(raw):
                if raw[position] == "\\":
                    position += 2
                elif raw[position] == '"':
                    position += 1
                    break
                else:
                    position += 1
        else:
            while position < len(raw) and raw[position] not in " \t\r\n,]}":
                position += 1
        after = position
        while after < len(raw) and raw[after] in " \t\r\n":
            after += 1
        lexical.append((("memory-own-journal", identity,
            "key-decode" if after < len(raw) and raw[after] == ":" else "token-decode"),
            _native_memory_lexical_token_upper(raw, start)))
    if depth != 0:
        raise HeaderBoundsError("memory_current_prior_journal_unavailable")
    def journal(current_only):
        result = [(("memory-reference-journal", "decode"), w["journal_upper"]),
            (("memory-reference-journal", "found-map"), 2)]
        for is_current, entries in ((False, original), (True, current)):
            if current_only and not is_current:
                continue
            identifier = MEMORY_CURRENT_CHECKPOINT if is_current else MEMORY_ORIGINAL_CHECKPOINT
            result.append((("memory-reference-journal", "record-field-set", identifier),
                5 + sum(width(name) for name in ("checkpoint_id", "safe", "state_digest", "payload"))))
            result.extend(entries)
            result.extend((("memory-reference-journal", stage, identifier),
                w["current_upper" if is_current else "original_upper"])
                for stage in ("digest-canonical", "digest-encoded"))
            result.append((("memory-reference-journal", "digest-result", identifier), 66))
        result.append((("memory-reference-journal", "return"), 4))
        return tuple(result)
    splice = [(("memory-current-splice", "journal-decode"), w["journal_upper"]), *current]
    splice.extend((("memory-current-splice", stage), upper) for stage, upper in (
        ("payload-canonical", w["current_upper"]), ("payload-encoded", w["current_upper"]),
        ("payload-digest", 66), ("record-canonical", w["current_record_upper"]),
        ("wrapper-copy", w["current_record_upper"] + 2)))
    splice.extend(journal(True))
    splice.append((("memory-current-splice", "span-list"), w["journal_upper"]))
    splice.extend((("memory-current-splice", "record-decode"), upper)
        for upper in (*prior_records, w["original_record_upper"]))
    splice.extend((("memory-current-splice", stage), upper) for stage, upper in (
        ("matches", w["journal_upper"]), ("prefix-copy", w["journal_upper"]),
        ("suffix-copy", w["journal_upper"]), ("join-copy", w["journal_upper"] + w["current_record_upper"] + 1),
        ("size-utf8", w["journal_upper"] + w["current_record_upper"] + 1)))
    result = list(_native_memory_current_allocation_entries(identity, row_upper=w["row_upper"],
        payload_upper=w["current_upper"], rows_upper=w["rows_upper"],
        record_upper=w["current_record_upper"], stage="setup"))
    for _ in range(33):
        result.extend(_native_memory_current_allocation_entries(identity, row_upper=max(w["own_raw_upper"], w["own_upper"]),
            payload_upper=w["current_upper"], rows_upper=w["rows_upper"],
            record_upper=w["current_record_upper"], stage="build"))
        result.extend(current)
        result.extend(splice)
        result.append((None, w["own_raw_upper"]))
    result.extend(_native_memory_current_allocation_entries(identity, row_upper=w["own_raw_upper"],
        payload_upper=w["current_upper"], rows_upper=w["rows_upper"],
        record_upper=w["current_record_upper"], stage="finish"))
    result.append((None, w["own_raw_upper"]))
    for stage in ("decode", "own-list", "replacement-map", "span-list", "span-sort"):
        result.append((("memory-own-journal", identity, stage), w["journal_upper"]))
    result.extend(journal(False))
    result.extend(lexical)
    events = tuple(address for address in new_addresses if address[0] in {"audit_events", "work_board_events"})
    key_upper = max(width(name) for name in (*_MEMORY_ORIGINAL_FIELDS, *_MEMORY_CURRENT_FIELDS,
        "checkpoint_id", "safe", "state_digest", "payload", "ref", "table", "key", "tuple_digest", "encoded_bytes"))
    scalar_upper = max(w["source_upper"], w["original_upper"], 122,
        *(max(width(table), 22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)) for table, key in rows))
    result.extend((("memory-own-journal", identity, "key-decode"), key_upper)
        for _ in range(42 + 9 * len(rows) + 2 * len(events)))
    result.extend((("memory-own-journal", identity, "token-decode"), scalar_upper)
        for _ in range(33 + 8 * len(rows) + 2 * len(events)))
    for _ in range(3):
        result.extend((("memory-own-journal", identity, stage), w["journal_upper"])
            for stage in ("prefix-copy", "suffix-copy", "join-copy"))
    maxima = {}
    for appearance, upper in result:
        maxima[appearance] = max(upper, maxima.get(appearance, 0))
    result = tuple((appearance, maxima[appearance]) for appearance, _upper in result)
    if any(type(upper) is not int or not 0 <= upper <= MAX_BYTES for _appearance, upper in result):
        raise HeaderBoundsError("canonical_bound_not_certified")
    return result


def _native_memory_current_lifecycle_numeric_entries(operation, prepared, *, certificate,
        retention_widths):
    """Two own Current builders, retaining completion's old Current decoder.

    Capture splices the original journal plus Original. Completion splices
    that same journal plus both Original and the actual capture Current. The
    unchanged splice decodes every existing record on each guarded build.
    The prior Current is therefore one extra decoder appearance per build;
    it is not a new physical reference or a second Current authority.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    entries = _native_memory_current_construction_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=retention_widths)
    record_upper = retention_widths["current_record_upper"]
    if type(record_upper) is not int or not 0 <= record_upper <= MAX_BYTES:
        raise HeaderBoundsError("canonical_bound_not_certified")
    # The original 32-iteration guard and final build remain unchanged.
    # This conservative forecast retains all 33 build liabilities in each
    # owner call; no successful-convergence discount is applied here.
    return tuple((phase, appearance, upper)
        for phase in ("capture", "completion")
        for appearance, upper in entries) + tuple(
        ("completion", ("memory-current-splice", "record-decode"), record_upper)
        for _ in range(33))


def _native_memory_six_current_construction_numeric_entries(operation, prepared, *, certificate, retention_widths, _construction_only=False):
    """ONE own Current construction, constructing six guarded build liabilities directly.

    Supplied-current-certificate numeric facts only. This excludes certificate
    acquisition, capture, publication and subsequent cold body readers.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width as width
    from src.runtime_plugins import memory_producer as source_owner
    if (type(operation) is not source_owner._MemoryOwnerSourceOperation
            or source_owner._MEMORY_OPERATIONS.get(id(operation)) is not operation):
        raise HeaderBoundsError("memory_current_numeric_owner_unavailable")
    guard = operation.db.info.get("composition_guard")
    facts = guard._native_memory_source_capacity_facts[3]
    source_owner._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    if (guard.header_budget is not certificate.budget
            or source_owner._native_memory_source_numeric_frame() is not certificate.budget):
        raise HeaderBoundsError("memory_current_convergence_owner_unavailable")
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_current_construction_mode_unavailable")
    w = retention_widths
    if _construction_only:
        from src.memory.header_bounds import MAX_ROWS
        source_owner._native_memory_dispatch_validation_numeric_owner(operation, prepared,
            certificate=certificate, facts=facts, retention_widths=w)
        from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
        source = operation.source
        if (source_owner._MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
                or type(certificate) is not CompositionHeaderCertificate
                or certificate.seal is not _SEAL or certificate.issued_id != id(certificate)
                or certificate.driver is not operation.driver
                or certificate.metadata_budget is not guard.header_budget
                or guard.header_budget is not certificate.budget
                or source_owner._native_memory_source_numeric_frame() is not certificate.budget):
            raise HeaderBoundsError("memory_current_convergence_owner_unavailable")
        selected = guard._native_memory_existing_source_rows
        projection = guard._native_memory_source_projection
        if selected[0] is not operation or selected[1] is not certificate or projection[0] is not operation:
            raise HeaderBoundsError("memory_current_convergence_owner_unavailable")
        own = None
        raw = None
        for address in facts["raw_tuple_uppers"]:
            if address[0] == "workflow_run_states" and address[1] == operation.run.run_identity:
                own = address
        for descriptor, key, row, encoded in selected[2]:
            if descriptor.table == "workflow_run_states" and key == operation.run.run_identity:
                raw = row["checkpoint_receipts_json"]
        if own is None or type(raw) is not str:
            raise HeaderBoundsError("memory_current_prior_journal_unavailable")
        identity = own[1]
        raw_total = 0
        for address in facts["raw_tuple_uppers"]:
            raw_total += facts["raw_tuple_uppers"][address]
        raw_total = raw_total - facts["raw_tuple_uppers"][own] + w["own_raw_upper"]
        row_count = len(facts["raw_tuple_uppers"])
        event_count = 0
        wrs_count = 0
        for address in facts["raw_tuple_uppers"]:
            if address[0] == "workflow_run_states":
                wrs_count += 1
        for descriptor, key, row in projection[1]:
            if descriptor.table in ("audit_events", "work_board_events"):
                event_count += 1
        if (not 1 <= row_count <= MAX_ROWS or wrs_count != 1
                or own[0] != "workflow_run_states" or own[1] != operation.run.run_identity
                or not 0 <= raw_total <= MAX_BYTES or w["own_raw_upper"] > MAX_BYTES
                or max(w["original_upper"], w["current_upper"]) > 262144):
            raise HeaderBoundsError("memory_current_successful_recurrence_unavailable")
        # SAME paid-column lexical traversal as the default branch, retaining
        # only scalar counts. No lexical/record/vector/container is built.
        position, depth, prior_count, lexical_count = 0, 0, 0, 0
        while position < len(raw):
            char = raw[position]
            if char in " \t\r\n,:":
                position += 1
                continue
            if char in "[{":
                if char == "{" and depth == 1:
                    prior_count += 1
                depth += 1
                position += 1
                continue
            if char in "]}":
                depth -= 1
                position += 1
                continue
            if char == '"':
                position += 1
                while position < len(raw):
                    if raw[position] == "\\":
                        position += 2
                    elif raw[position] == '"':
                        position += 1
                        break
                    else:
                        position += 1
            else:
                while position < len(raw) and raw[position] not in " \t\r\n,]}":
                    position += 1
            lexical_count += 1
        if depth != 0:
            raise HeaderBoundsError("memory_current_prior_journal_unavailable")
        current_count = 18 + 9 * row_count + 4 * event_count
        original_count = 18 + 4 * row_count
        current_journal_count = 7 + current_count
        both_journal_count = 11 + original_count + current_count
        splice_count = 20 + 2 * current_count + prior_count
        selected_count = (302 + 19 * current_count + original_count
            + 17 * row_count + 4 * event_count + 6 * prior_count + lexical_count)
        # Numeric planning creates owner tuples, not serialized bodies. Its
        # immutable Source-created names/identity/appearance references retain
        # the existing opaque-pointer width4; integer slots retain width20.
        # Actual JSON/codec/copy appearances stay fully priced in the vector.
        # A newly constructed label has <=3 pointers:16; a pair is27; a
        # lifecycle triplet is32. List/tuple pointer slots cost5; a maxima
        # dictionary key-pointer/int slot costs26. No label/identity is copied.
        preflight_storage = 20 + 106 * (current_count + original_count) + 10 * (row_count + event_count)
        lexical_storage = 4 + 48 * lexical_count + 21 * prior_count
        journal_storage = 8 + 53 * (current_journal_count + both_journal_count)
        splice_storage = 2 + 5 * splice_count + 43 * (13 + prior_count)
        tail_new_pairs = 89 + 17 * row_count + 4 * event_count
        result_storage = 6 + 63 * selected_count + 43 * tail_new_pairs + 7 * 27
        # Eight allocation helpers construct all25 original stage pairs and
        # three stage tuples/maps before selecting setup8/build10/finish7.
        # 8calls*(25pairs27 +25pointer slots5+6 +dict32 +argtuple86 +base2)
        # plus75 returned label/pair/tuple slots48.
        allocation_storage = 8 * (25 * 27 + 25 * 5 + 6 + 32 + 86 + 2) + 75 * 48
        # The starred scalar-generator tuple and combined max-argument tuple
        # are distinct: preserve the extra2+21R before constructing either.
        auxiliary_storage = 2 + 5 * event_count + 197 + 65 * row_count + 65 + 2 + 21 * row_count
        # First2N-triplet tuple, separate6 completion-decoder tuple, and final
        # concatenated2N+6-pointer tuple ALL remain distinct allocations.
        lifecycle_storage = 258 + 84 * selected_count
        # Fixed64 original generator/function/context owner slots, each<=32,
        # cover scalar-control bookkeeping; no data-dependent storage hidden.
        return (preflight_storage + lexical_storage + journal_storage + splice_storage
            + result_storage + allocation_storage + auxiliary_storage + lifecycle_storage + 2048
            + 4 * (1024 + 128 * row_count))

    _guard, rows, own, audit, new_addresses, value = _native_memory_generic_future_bound_inputs(
        operation, prepared, certificate, facts, retention_widths)
    identity = own[1]
    raw = value(own, "checkpoint_receipts_json")
    if type(raw) is not str:
        raise HeaderBoundsError("memory_current_prior_journal_unavailable")
    current = _native_memory_reference_preflight_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=w, current=True)
    original = _native_memory_original_preflight_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=w)
    # Structural lexical arithmetic on an already-paid strict original column.
    # Retain numerical token/record widths; no decoded record or future body.
    lexical, prior_records = [], []
    position, depth = 0, 0
    while position < len(raw):
        char = raw[position]
        if char in " \t\r\n,:":
            position += 1
            continue
        if char in "[{":
            if char == "{" and depth == 1:
                prior_records.append(_native_memory_lexical_token_upper(raw, position))
            depth += 1
            position += 1
            continue
        if char in "]}":
            depth -= 1
            position += 1
            continue
        start = position
        if char == '"':
            position += 1
            while position < len(raw):
                if raw[position] == "\\":
                    position += 2
                elif raw[position] == '"':
                    position += 1
                    break
                else:
                    position += 1
        else:
            while position < len(raw) and raw[position] not in " \t\r\n,]}":
                position += 1
        after = position
        while after < len(raw) and raw[after] in " \t\r\n":
            after += 1
        lexical.append((("memory-own-journal", identity,
            "key-decode" if after < len(raw) and raw[after] == ":" else "token-decode"),
            _native_memory_lexical_token_upper(raw, start)))
    if depth != 0:
        raise HeaderBoundsError("memory_current_prior_journal_unavailable")
    def journal(current_only):
        result = [(("memory-reference-journal", "decode"), w["journal_upper"]),
            (("memory-reference-journal", "found-map"), 2)]
        for is_current, entries in ((False, original), (True, current)):
            if current_only and not is_current:
                continue
            identifier = MEMORY_CURRENT_CHECKPOINT if is_current else MEMORY_ORIGINAL_CHECKPOINT
            result.append((("memory-reference-journal", "record-field-set", identifier),
                5 + sum(width(name) for name in ("checkpoint_id", "safe", "state_digest", "payload"))))
            result.extend(entries)
            result.extend((("memory-reference-journal", stage, identifier),
                w["current_upper" if is_current else "original_upper"])
                for stage in ("digest-canonical", "digest-encoded"))
            result.append((("memory-reference-journal", "digest-result", identifier), 66))
        result.append((("memory-reference-journal", "return"), 4))
        return tuple(result)
    splice = [(("memory-current-splice", "journal-decode"), w["journal_upper"]), *current]
    splice.extend((("memory-current-splice", stage), upper) for stage, upper in (
        ("payload-canonical", w["current_upper"]), ("payload-encoded", w["current_upper"]),
        ("payload-digest", 66), ("record-canonical", w["current_record_upper"]),
        ("wrapper-copy", w["current_record_upper"] + 2)))
    splice.extend(journal(True))
    splice.append((("memory-current-splice", "span-list"), w["journal_upper"]))
    splice.extend((("memory-current-splice", "record-decode"), upper)
        for upper in (*prior_records, w["original_record_upper"]))
    splice.extend((("memory-current-splice", stage), upper) for stage, upper in (
        ("matches", w["journal_upper"]), ("prefix-copy", w["journal_upper"]),
        ("suffix-copy", w["journal_upper"]), ("join-copy", w["journal_upper"] + w["current_record_upper"] + 1),
        ("size-utf8", w["journal_upper"] + w["current_record_upper"] + 1)))
    result = list(_native_memory_current_allocation_entries(identity, row_upper=w["row_upper"],
        payload_upper=w["current_upper"], rows_upper=w["rows_upper"],
        record_upper=w["current_record_upper"], stage="setup"))
    for _ in range(6):
        result.extend(_native_memory_current_allocation_entries(identity, row_upper=max(w["own_raw_upper"], w["own_upper"]),
            payload_upper=w["current_upper"], rows_upper=w["rows_upper"],
            record_upper=w["current_record_upper"], stage="build"))
        result.extend(current)
        result.extend(splice)
        result.append((None, w["own_raw_upper"]))
    result.extend(_native_memory_current_allocation_entries(identity, row_upper=w["own_raw_upper"],
        payload_upper=w["current_upper"], rows_upper=w["rows_upper"],
        record_upper=w["current_record_upper"], stage="finish"))
    result.append((None, w["own_raw_upper"]))
    for stage in ("decode", "own-list", "replacement-map", "span-list", "span-sort"):
        result.append((("memory-own-journal", identity, stage), w["journal_upper"]))
    result.extend(journal(False))
    result.extend(lexical)
    events = tuple(address for address in new_addresses if address[0] in {"audit_events", "work_board_events"})
    key_upper = max(width(name) for name in (*_MEMORY_ORIGINAL_FIELDS, *_MEMORY_CURRENT_FIELDS,
        "checkpoint_id", "safe", "state_digest", "payload", "ref", "table", "key", "tuple_digest", "encoded_bytes"))
    scalar_upper = max(w["source_upper"], w["original_upper"], 122,
        *(max(width(table), 22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)) for table, key in rows))
    result.extend((("memory-own-journal", identity, "key-decode"), key_upper)
        for _ in range(42 + 9 * len(rows) + 2 * len(events)))
    result.extend((("memory-own-journal", identity, "token-decode"), scalar_upper)
        for _ in range(33 + 8 * len(rows) + 2 * len(events)))
    for _ in range(3):
        result.extend((("memory-own-journal", identity, stage), w["journal_upper"])
            for stage in ("prefix-copy", "suffix-copy", "join-copy"))
    maxima = {}
    for appearance, upper in result:
        maxima[appearance] = max(upper, maxima.get(appearance, 0))
    result = tuple((appearance, maxima[appearance]) for appearance, _upper in result)
    if any(type(upper) is not int or not 0 <= upper <= MAX_BYTES for _appearance, upper in result):
        raise HeaderBoundsError("canonical_bound_not_certified")
    return result


def _native_memory_successful_current_lifecycle_numeric_entries(operation, prepared, *, certificate,
        retention_widths):
    """Successful fixed-field Current liability: six builds and normalization.

    The original builder still has its 32-iteration fail-closed guard. This
    forecast applies only to the Source's closed, one-own-WRS projection. In
    that projection the normalized own digest is constant; the only variable
    serialized scalars are the own encoded count and the total encoded count.
    R403 bounds convergence by five loop builds, followed by the final build.
    Every allocation, parser and splice of those six builds is retained.
    This is not an authority to execute an unsupported or failed projection.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, MAX_ROWS
    from src.runtime_plugins import memory_producer as source_owner
    if (type(operation) is not source_owner._MemoryOwnerSourceOperation
            or source_owner._MEMORY_OPERATIONS.get(id(operation)) is not operation):
        raise HeaderBoundsError("memory_current_convergence_owner_unavailable")
    guard = operation.db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_source_capacity_facts", None)
    if retained is None or retained[0] is not operation or retained[1] is not prepared:
        raise HeaderBoundsError("memory_current_convergence_owner_unavailable")
    facts = retained[3]
    source_owner._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    if (guard.header_budget is not certificate.budget
            or source_owner._native_memory_source_numeric_frame() is not certificate.budget):
        raise HeaderBoundsError("memory_current_convergence_owner_unavailable")
    _guard, rows, own, _audit, _new, _value = _native_memory_generic_future_bound_inputs(
        operation, prepared, certificate, facts, retention_widths)
    w = retention_widths
    # These are original schema maxima, not a new convergence guard. The
    # seven-digit count proof fails if the complete selected tuple capacity
    # exceeds the original frame or an envelope cannot be preflighted.
    raw_total = (sum(facts["raw_tuple_uppers"].values())
        - facts["raw_tuple_uppers"][own] + w["own_raw_upper"])
    if (not 1 <= len(rows) <= MAX_ROWS
            or sum(address[0] == "workflow_run_states" for address in rows) != 1
            or own != ("workflow_run_states", operation.run.run_identity)
            or raw_total > MAX_BYTES
            or w["own_raw_upper"] > MAX_BYTES
            or max(w["original_upper"], w["current_upper"]) > 262144):
        raise HeaderBoundsError("memory_current_successful_recurrence_unavailable")
    selected = _native_memory_six_current_construction_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=w)
    build_marker = ("memory-current-allocation", own[1], "count-update")
    finish_marker = ("memory-current-allocation", own[1], "projection-copy")
    if (sum(appearance == build_marker for appearance, upper in selected) != 6
            or sum(appearance == finish_marker for appearance, upper in selected) != 1
            or sum(appearance is None for appearance, upper in selected) != 7):
        raise HeaderBoundsError("memory_current_source_tuple_grammar_changed")
    record_upper = w["current_record_upper"]
    return tuple((phase, appearance, upper)
        for phase in ("capture", "completion")
        for appearance, upper in selected) + tuple(
        ("completion", ("memory-current-splice", "record-decode"), record_upper)
        for _ in range(6))


def _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, widths):
    """Authenticate numeric Source facts; this grants no future read permission."""
    import asyncio
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, MAX_ROWS, COMPOSITION_DESCRIPTORS
    from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
    from src.memory.m5 import PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS
    from src.runtime_plugins.memory_producer import (
        _MemoryOwnerSourceOperation, _MEMORY_OPERATIONS, _MEMORY_SOURCES, _MEMORY_SOURCE_SEAL)
    guard = operation.db.info.get("composition_guard") if type(operation) is _MemoryOwnerSourceOperation else None
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    retained = getattr(guard, "_native_memory_source_capacity_facts", None)
    retention = getattr(guard, "_native_memory_retention_numeric_facts", None)
    baseline = getattr(guard, "_native_memory_baseline_numeric", None)
    selected = getattr(guard, "_native_memory_existing_source_rows", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    proofs = getattr(guard, "_native_memory_retained_proofs", None)
    if (type(guard) is not CompositionSessionGuard
            or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or _MEMORY_SOURCES.get(id(operation.source)) is not operation.source
            or operation._seal is not _MEMORY_SOURCE_SEAL or operation._issued_id != id(operation)
            or operation.source._seal is not _MEMORY_SOURCE_SEAL
            or operation.source._issued_id != id(operation.source) or not operation.source._live.is_set()
            or operation.scope is not operation.source._scope
            or operation.source.claim._host is not operation.source.host
            or not operation.source.host.admitting or operation.source.host.reviewed is None
            or operation.source.host.boot_nonce != operation.scope.host_boot_nonce
            or operation.source.claim.host_boot_nonce != operation.scope.host_boot_nonce
            or operation.source.host.reviewed.package_digest != operation.scope.witness["package_digest"]
            or operation.source.host.reviewed.composition_digest != operation.scope.witness["host_composition_digest"]
            or operation.run.run_identity != operation.source.claim.job["job_id"]
            or operation.scope.binding is not operation.source.claim.binding
            or type(prepared) is not PreparedNativeMemoryMutation
            or _NATIVE_MUTATION_PLANS.get(id(prepared)) is not prepared
            or operation._prepared is not prepared or prepared.consumed or operation._effect is not None
            or type(certificate) is not CompositionHeaderCertificate or certificate.seal is not _SEAL
            or certificate.issued_id != id(certificate) or certificate.budget is not guard.header_budget
            or certificate.driver is not operation.driver
            or certificate.metadata_budget is not guard.header_budget
            or certificate.common_certificate is not None
            or operation.source.admission.header_budget is not guard.header_budget
            or guard.db is not operation.db or not operation.driver.in_transaction
            or owner is None or len(owner) != 6 or owner[0] is not operation
            or owner[1] is not prepared or owner[2] is not certificate
            or owner[3] is not asyncio.current_task()
            or owner[4] is not operation.transaction
            or owner[4] is not operation.db.sync_session.get_transaction()
            or owner[5] is not operation.driver
            or type(facts) is not MappingProxyType or type(widths) is not MappingProxyType
            or retained is None or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not facts
            or retention is None or retention[0] is not operation or retention[1] is not prepared
            or retention[2] is not certificate or retention[3] is not widths
            or baseline is None or baseline[0] is not operation or baseline[1] is not certificate
            or facts.get("baseline_trace") is not baseline[2]
            or selected is None or selected[0] is not operation
            or projection is None or projection[0] is not operation
            or proofs is None or proofs[0] is not operation):
        raise HeaderBoundsError("memory_future_body_owner_changed")
    # Invalidating another retained Current changes that owner's WRS grammar.
    # Only the original owner's exact paid proof cohort can supply that bound.
    if proofs[2]:
        affected = getattr(guard, "_native_memory_affected_numeric_facts", None)
        if (affected is None or affected[0] is not operation or affected[1] is not prepared
                or affected[2] is not certificate or affected[3] is not proofs[2]
                or type(affected[4]) is not MappingProxyType
                or set(affected[4]) != {proof.before["run_identity"] for proof in proofs[2]}
                or any(type(dimensions) is not MappingProxyType
                    or any(type(amount) is not int or not 0 <= amount <= MAX_BYTES
                        for name, amount in dimensions.items() if name != "future_text_bytes")
                    or type(dimensions.get("future_text_bytes")) is not MappingProxyType
                    for dimensions in affected[4].values())):
            raise HeaderBoundsError("memory_future_body_affected_unknown_width_unavailable")
    needed = {"row_upper", "own_upper", "own_raw_upper", "context_upper", "arguments_upper",
        "authority_upper", "journal_upper", "binding_text_bytes", "effect_upper", "source_upper",
        "original_upper", "original_record_upper", "current_upper", "current_record_upper",
        "rows_upper", "row_count", "refs_upper", "selected_rows_upper", "own_journal_upper",
        "audit_upper", "details_upper", "future_text_bytes", "completion_future_text_bytes", "claimed", "result_upper"}
    if (set(widths) != needed or widths["claimed"] is not True
            or type(widths["future_text_bytes"]) is not MappingProxyType
            or set(widths["future_text_bytes"]) != {"checkpoint_context_json", "checkpoint_receipts_json", "updated_at"}
            or any(type(name) is not str or type(amount) is not int or not 0 <= amount <= MAX_BYTES
                for name, amount in widths["future_text_bytes"].items())
            or type(widths["completion_future_text_bytes"]) is not MappingProxyType
            or set(widths["completion_future_text_bytes"]) != {"checkpoint_context_json", "checkpoint_receipts_json",
                "updated_at", "status", "result_digest", "result_summary", "finished_at"}
            or any(type(name) is not str or type(amount) is not int or not 0 <= amount <= MAX_BYTES
                for name, amount in widths["completion_future_text_bytes"].items())
            or any(type(widths[name]) is not int or not 0 <= widths[name] <= MAX_BYTES
                for name in needed - {"future_text_bytes", "completion_future_text_bytes", "claimed"})
            or not 1 <= widths["row_count"] <= MAX_ROWS
            or any(type(facts.get(name)) is not MappingProxyType
                for name in ("row_uppers", "raw_tuple_uppers", "text_bytes"))):
        raise HeaderBoundsError("memory_future_body_numeric_shape_unavailable")
    rows = {}
    for descriptor, key, row, _encoded in selected[2]:
        address = (descriptor.table, key)
        if descriptor is not COMPOSITION_DESCRIPTORS.get(descriptor.table) or type(row) is not dict:
            raise HeaderBoundsError("memory_future_body_projection_changed")
        changes = next((values for changed, address_key, values in projection[2]
            if changed is descriptor and address_key == key), None)
        # Read the sealed original mapping and scalar overrides without a future row.
        rows[address] = (row, changes)
    for descriptor, key, row in projection[1]:
        address = (descriptor.table, key)
        if descriptor is not COMPOSITION_DESCRIPTORS.get(descriptor.table) or address in rows or type(row) is not dict:
            raise HeaderBoundsError("memory_future_body_projection_changed")
        if type(key) is _NativeMemoryFutureIntegerSlot:
            if (_MEMORY_INTEGER_SLOTS.get(id(key)) is not key or key.operation is not operation
                    or key.prepared is not prepared or key.descriptor is not descriptor
                    or key.task is not asyncio.current_task() or key.db is not operation.db
                    or key.transaction is not operation.transaction or key.driver is not operation.driver
                    or key.budget is not guard.header_budget or key.bound_key is not None):
                raise HeaderBoundsError("memory_future_integer_slot_changed")
        elif type(key) is not str or not key:
            raise HeaderBoundsError("memory_future_body_projection_changed")
        if descriptor.table not in {"audit_events", "memories", "memory_proposals",
                "work_board_decision_receipts", "work_board_events"}:
            raise HeaderBoundsError("memory_future_body_new_graph_unsupported")
        rows[address] = (row, None)
    if (set(rows) != set(facts["row_uppers"]) or set(rows) != set(facts["raw_tuple_uppers"])
            or set(rows) != set(facts["text_bytes"]) or len(rows) != widths["row_count"]
            or not facts["selected_addresses"] <= facts["baseline_members"]):
        raise HeaderBoundsError("memory_future_body_unpaid_predecessor")
    own = ("workflow_run_states", operation.run.run_identity)
    if own not in rows or own not in facts["baseline_members"]:
        raise HeaderBoundsError("memory_future_body_own_root_unavailable")
    def value(address, name):
        row, changes = rows[address]
        return changes[name] if changes is not None and name in changes else row[name]
    audit = prepared.audit_event.id if prepared.audit_event is not None else prepared._result.get("audit_event_id")
    audit_address = ("audit_events", audit)
    new_addresses = frozenset((descriptor.table, key) for descriptor, key, _row in projection[1])
    if (audit_address not in new_addresses or audit_address in facts["baseline_members"]
            or any(address[0] == "audit_events" and address != audit_address for address in new_addresses)
            or ("sessions", value(audit_address, "session_id")) not in facts["baseline_members"]):
        raise HeaderBoundsError("memory_future_body_audit_graph_unsupported")
    for address in new_addresses:
        if address[0] == "work_board_events" and ("work_board_tasks", value(address, "task_id")) not in facts["selected_addresses"]:
            raise HeaderBoundsError("memory_future_body_unpaid_predecessor")
    return guard, rows, own, audit_address, new_addresses, value


def _native_memory_postoriginal_cold_numeric_entries(operation, prepared, *, certificate, facts, retention_widths):
    """One successful own-root cold inspection, including its repeated readers."""
    from src.memory.header_bounds import (HeaderBoundsError, MEMORY_DESCRIPTORS,
        _native_memory_ascii_string_width as width, _native_memory_utf8_width,
        _native_memory_object_width as obj)
    from src.workflows.inference_accounting import _original_native_binding_numeric_entries
    guard, rows, own, audit, new_addresses, value = _native_memory_generic_future_bound_inputs(
        operation, prepared, certificate, facts, retention_widths)
    w, run_id = retention_widths, own[1]
    entries = list(_native_memory_cold_candidate_numeric_entries(run_id,
        row_upper=w["row_upper"], context_upper=w["context_upper"],
        arguments_upper=w["arguments_upper"], authority_upper=w["authority_upper"]))
    from src.runtime_plugins.memory_producer import original_native_memory_source_context_numeric_entries
    locator_upper = obj({"profile": width(MEMORY_REFERENCE_PROFILE),
        "invocation_ref": width(run_id), "original_checkpoint_id": width(MEMORY_ORIGINAL_CHECKPOINT),
        "original_checkpoint_digest": 66})
    entries.extend(original_native_memory_source_context_numeric_entries(
        context_upper=w["context_upper"], candidate_upper=w["source_upper"],
        binding_text_bytes=w["binding_text_bytes"], result_upper=w["result_upper"],
        effect_upper=w["effect_upper"], locator_upper=locator_upper, result_present=True))
    for _ in range(2):  # direct candidate binding and nested Original claim
        entries.extend(_original_native_binding_numeric_entries(run_id, w["binding_text_bytes"]))
    for owner, count in (("candidate", 2), ("claim", 1), ("original", 2)):
        entries.extend(_native_memory_cold_binding_digest_numeric_entries(run_id,
            w["binding_text_bytes"], owner=owner, occurrences=count))
    entries.extend(_native_memory_cold_claim_numeric_entries(run_id, w["journal_upper"], claimed=True))
    entries.extend(_native_memory_cold_original_numeric_entries(run_id, effect_upper=w["effect_upper"],
        source_upper=w["source_upper"], original_upper=w["original_record_upper"]))
    entries.extend(_native_memory_cold_reference_numeric_entries(run_id,
        rows_upper=w["rows_upper"], row_count=w["row_count"], selected_rows_upper=w["selected_rows_upper"],
        own_upper=w["own_upper"], own_journal_upper=w["own_journal_upper"]))
    entries.extend(_native_memory_cold_audit_numeric_entries(run_id,
        audit_upper=w["audit_upper"], details_upper=w["details_upper"]))
    entries.append((None, w["own_raw_upper"]))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-output",
        w["context_upper"] + width(audit[1]) + 72, ("audit-tuple", "result-tuple")))
    # Runtime constructors are future liabilities distinct from this paid
    # forecast construction. Preserve the actual separate claim6/claim11 and
    # parser2/digest2,1,2 appearances; no callback count is collapsed.
    entries.append((("native-memory-cold-construction", "candidate", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=33, groups=6, sizing_fields=4)))
    for _ in range(2):
        entries.append((("native-memory-cold-construction", "binding-parser", run_id),
            _native_memory_cold_binding_construction_upper(run_id)))
    for owner, count in (("binding-digest-candidate", 2), ("binding-digest-claim", 1),
            ("binding-digest-original", 2)):
        entries.append((("native-memory-cold-construction", owner, run_id),
            _native_memory_cold_binding_construction_upper(run_id, occurrences=count, digest_only=True)))
    entries.append((("native-memory-cold-construction", "claim", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=6, groups=1)))
    entries.append((("native-memory-cold-construction", "claim", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=11, groups=1)))
    entries.append((("native-memory-cold-construction", "original", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=21, groups=5, sizing_fields=2)))
    entries.append((("native-memory-cold-construction", "current-reference", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=1, groups=1)))
    entries.append((("native-memory-cold-construction", "own-reference-sizing", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=0, groups=1, sizing_fields=2)))
    entries.append((("native-memory-cold-construction", "audit", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=6, groups=3, sizing_fields=5)))
    entries.append((("native-memory-cold-construction", "reference", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=21, groups=8, sizing_fields=2)))
    entries.append((("native-memory-cold-construction", "reference-slice", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=20, groups=1)))
    entries.append((("native-memory-cold-construction", "current-output", run_id),
        _native_memory_cold_vector_construction_upper(run_id, entries=2, groups=1)))
    refs = tuple(rows)
    pair_upper = sum(3 + width(table) + (22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)) + 1
        for table, key in refs) + 2
    events = tuple(address for address in new_addresses if address[0] in {"audit_events", "work_board_events"})
    event_pair_upper = sum(3 + width(table) + (22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)) + 1
        for table, key in events) + 2
    events_upper = 2 + sum(obj({"table": width(table), "key":
        22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)}) + 1 for table, key in events)
    def key_set(names):
        return 2 + sum(width(name) + 1 for name in names)
    def payload(current):
        result = []
        prefix = ("memory-reference-payload", "current" if current else "original")
        def pay(stage, amount):
            result.append(((*prefix, stage), amount))
        def reference(address):
            table, key = address
            pay("reference-field-set", key_set(("table", "key")))
            pay("reference-table-set", 2 + sum(width(name) + 1 for name in RETAINED_FIELDS)
                + sum(width(name) + 1 for name in MEMORY_DESCRIPTORS))
            pay("key-utf8", 20 if type(key) is _NativeMemoryFutureIntegerSlot else _native_memory_utf8_width(key))
            pay("reference-tuple", 3 + width(table) + (22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)))
        pay("payload-field-set", key_set(_MEMORY_CURRENT_FIELDS if current else _MEMORY_ORIGINAL_FIELDS))
        for stage in ("reference-list", "reference-set", "reference-sort"):
            pay(stage, w["refs_upper"])
        for address in refs:
            reference(address)
        if current:
            for stage in ("reference-list", "reference-set", "reference-sort"):
                pay(stage, events_upper)
            for address in events:
                reference(address)
            pay("absence-list", 2)
            for stage in ("absence-set", "absence-sort", "reference-union-tuple", "reference-union-set"):
                pay(stage, pair_upper + event_pair_upper)
            pay("row-reference-list", w["rows_upper"])
            for address in refs:
                pay("row-field-set", key_set(("ref", "tuple_digest", "encoded_bytes")))
                reference(address)
            for stage in ("row-reference-union", "row-reference-set", "row-reference-sort"):
                pay(stage, pair_upper + event_pair_upper)
        else:
            for scalar in (run_id, operation.source._scope.witness["claim_ref"],
                    operation.source.operator.principal.principal_id, operation.source.operator.session_id):
                pay("key-utf8", _native_memory_utf8_width(scalar))
            pay("audit-field-set", key_set(("ref", "tuple_digest")))
            reference(audit)
            for stage in ("deadline-parse", "deadline-zone", "deadline-isoformat"):
                pay(stage, 34)
        for stage in ("canonical", "encoded"):
            pay(stage, w["current_upper" if current else "original_upper"])
        return result
    # The direct journal parser and the normalization parser each inspect both records.
    for _ in range(2):
        entries.extend(((("memory-reference-journal", "decode"), w["journal_upper"]),
            (("memory-reference-journal", "found-map"), 2)))
        for current, identifier in ((False, MEMORY_ORIGINAL_CHECKPOINT), (True, MEMORY_CURRENT_CHECKPOINT)):
            entries.append((("memory-reference-journal", "record-field-set", identifier),
                key_set(("checkpoint_id", "safe", "state_digest", "payload"))))
            entries.extend(payload(current))
            for stage in ("digest-canonical", "digest-encoded"):
                entries.append((("memory-reference-journal", stage, identifier),
                    w["current_upper" if current else "original_upper"]))
            entries.append((("memory-reference-journal", "digest-result", identifier), 66))
        entries.append((("memory-reference-journal", "return"), 4))
    for stage in ("decode", "own-list", "replacement-map", "span-list", "span-sort"):
        entries.append((("memory-own-journal", run_id, stage), w["own_journal_upper"]))
    for _ in range(3):
        for stage in ("prefix-copy", "suffix-copy", "join-copy"):
            entries.append((("memory-own-journal", run_id, stage), w["own_journal_upper"]))
    # Existing journal is an already-paid strict JSON string. Tokenize lexically;
    # no JSON object, future payload, reference or decoded token is constructed.
    raw = value(own, "checkpoint_receipts_json")
    position = 0
    while position < len(raw):
        char = raw[position]
        if char in " \t\r\n{}[],:":
            position += 1
            continue
        start = position
        if char == '"':
            position += 1
            while position < len(raw):
                if raw[position] == "\\":
                    position += 2
                elif raw[position] == '"':
                    position += 1
                    break
                else:
                    position += 1
        else:
            while position < len(raw) and raw[position] not in " \t\r\n,]}":
                position += 1
        after = position
        while after < len(raw) and raw[after] in " \t\r\n":
            after += 1
        stage = "key-decode" if after < len(raw) and raw[after] == ":" else "token-decode"
        entries.append((("memory-own-journal", run_id, stage), _native_memory_lexical_token_upper(raw, start)))
    literal_keys = (*_MEMORY_ORIGINAL_FIELDS, *_MEMORY_CURRENT_FIELDS,
        "checkpoint_id", "safe", "state_digest", "payload", "ref", "table", "key", "tuple_digest", "encoded_bytes")
    key_upper = max(width(name) for name in literal_keys)
    scalar_upper = max(122, 66, 34, width(MEMORY_REFERENCE_PROFILE), width(prepared._method),
        width(MEMORY_ORIGINAL_CHECKPOINT), width(MEMORY_CURRENT_CHECKPOINT), width("validated"),
        width(run_id), width(operation.source._scope.witness["claim_ref"]),
        width(operation.source.operator.principal.principal_id), width(operation.source.operator.session_id),
        *(max(width(table), 22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)) for table, key in refs))
    for _ in range(42 + 9 * len(refs) + 2 * len(events)):
        entries.append((("memory-own-journal", run_id, "key-decode"), key_upper))
    for _ in range(33 + 8 * len(refs) + 2 * len(events)):
        entries.append((("memory-own-journal", run_id, "token-decode"), scalar_upper))
    from src.extensions.capability_execution import original_native_memory_configured_mac_numeric_schedule
    for address in refs:
        if address[0] == "work_board_decision_receipts":
            proposal = ("memory_proposals", value(address, "source_proposal_id"))
            entries.append((("native-memory-cold-construction", "receipt", run_id),
                _native_memory_cold_vector_construction_upper(run_id, entries=21, groups=5, sizing_fields=33)))
            entries.extend(_native_memory_cold_receipt_numeric_entries(run_id,
                receipt_upper=facts["row_uppers"][address],
                proposal_upper=facts["row_uppers"].get(proposal, 0),
                evidence_upper=6 * facts["text_bytes"][address]["retrieval_evidence_ids_json"] + 2))
            entries.extend(original_native_memory_configured_mac_numeric_schedule(kind="effect"))
        if address[0] == "memory_proposals" and value(address, "status") == "accepted":
            memory = ("memories", value(address, "accepted_memory_id"))
            if memory not in rows:
                raise HeaderBoundsError("memory_future_body_accepted_memory_unavailable")
            entries.append((("native-memory-cold-construction", "selection", run_id),
                _native_memory_cold_vector_construction_upper(run_id, entries=51, groups=3, sizing_fields=11)))
            entries.extend(_native_memory_cold_selection_numeric_entries(run_id,
                memory_upper=facts["row_uppers"][memory], proposal_upper=facts["row_uppers"][address],
                metadata_upper=6 * facts["text_bytes"][memory]["metadata_json"] + 2,
                scope_upper=6 * facts["text_bytes"][address]["memory_scope_json"] + 2))
            for _ in range(2):
                entries.extend(original_native_memory_configured_mac_numeric_schedule(kind="effect"))
    return tuple(entries)


def _native_memory_standalone_cold_construction_numeric_upper(operation, prepared, *,
        certificate, facts, retention_widths):
    """Preconstruction capacity for ONE standalone original cold forecast.

    Generic BODY/common/Universe/SourceRows/witness builders and the caller's
    retained five-field tuple are separate. No forecast array/body/certificate
    is constructed here. Nonempty affected cohorts fail before allocation.
    """
    from src.runtime_plugins import memory_producer as source_owner
    from src.memory.header_bounds import HeaderBoundsError, MAX_ROWS, MEMORY_DESCRIPTORS
    from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
    source_owner._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard = operation.db.info.get("composition_guard")
    source = operation.source
    proofs = getattr(guard, "_native_memory_retained_proofs", None)
    selected = getattr(guard, "_native_memory_existing_source_rows", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    if (source_owner._MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or type(certificate) is not CompositionHeaderCertificate
            or certificate.seal is not _SEAL or certificate.issued_id != id(certificate)
            or certificate.driver is not operation.driver
            or certificate.metadata_budget is not certificate.budget
            or guard.header_budget is not certificate.budget
            or source_owner._native_memory_source_numeric_frame() is not certificate.budget
            or type(proofs) is not tuple or len(proofs) != 3 or proofs[0] is not operation
            or type(selected) is not tuple or len(selected) != 3 or selected[0] is not operation
            or selected[1] is not certificate or type(projection) is not tuple or len(projection) != 3
            or projection[0] is not operation):
        raise HeaderBoundsError("memory_cold_construction_numeric_owner_changed")
    if proofs[2]:
        raise HeaderBoundsError("memory_cold_construction_affected_unavailable")
    row_count = len(facts["row_uppers"])
    if not 1 <= row_count <= MAX_ROWS or retention_widths["row_count"] != row_count:
        raise HeaderBoundsError("memory_cold_construction_numeric_shape_unavailable")
    # Borrow only original already-paid selected/projection maps. Override
    # lookup uses scalar loops rather than the generic owner's new row map.
    raw = None
    event_count = receipt_count = accepted_count = 0
    for descriptor, key, row, encoded in selected[2]:
        if descriptor.table == "workflow_run_states" and key == operation.run.run_identity:
            raw = row["checkpoint_receipts_json"]
    for descriptor, key, changes in projection[2]:
        if (descriptor.table == "workflow_run_states" and key == operation.run.run_identity
                and "checkpoint_receipts_json" in changes):
            raw = changes["checkpoint_receipts_json"]
    for address in facts["row_uppers"]:
        table, key = address
        if table == "work_board_decision_receipts":
            receipt_count += 1
        if table == "memory_proposals":
            status = None
            for descriptor, row_key, row, encoded in selected[2]:
                if descriptor.table == table and row_key == key:
                    status = row["status"]
            for descriptor, row_key, row in projection[1]:
                if descriptor.table == table and row_key == key:
                    status = row["status"]
            for descriptor, row_key, changes in projection[2]:
                if descriptor.table == table and row_key == key and "status" in changes:
                    status = changes["status"]
            if status == "accepted":
                accepted_count += 1
    for descriptor, key, row in projection[1]:
        if descriptor.table in ("audit_events", "work_board_events"):
            event_count += 1
    if type(raw) is not str:
        raise HeaderBoundsError("memory_cold_construction_prior_journal_unavailable")
    # Original lexical parser appends one fee pair per strict JSON token. Count
    # the genuine borrowed column without substring/token/list construction.
    position = lexical_count = 0
    while position < len(raw):
        char = raw[position]
        if char in " \t\r\n{}[],:":
            position += 1
            continue
        lexical_count += 1
        if char == '"':
            position += 1
            while position < len(raw):
                if raw[position] == "\\":
                    position += 2
                elif raw[position] == '"':
                    position += 1
                    break
                else:
                    position += 1
        else:
            while position < len(raw) and raw[position] not in " \t\r\n,]}":
                position += 1
    # Cold fixed constructors: candidate33/context120/binding120/digest105/
    # claim17/original21/reference21/audit6/raw1/output2=446. Two journal
    # traversals add94+26R+8E; journal/lexical tail89+17R+4E+L.
    # Receipt21 + one configured MAC<=12; accepted selection51 + two MACs<=24.
    cold_count = (629 + 43 * row_count + 12 * event_count + lexical_count
        + 34 * receipt_count + 76 * accepted_count + 15)
    # Same immutable-reference4/int64 scalar20 planning convention as the
    # Current constructor. Label <=7 references =>30; pair <=53. Count label,
    # pair, list slot and tuple slot separately; no generic maximum map.
    # The original standalone builder calls the generic lineage/row-map
    # owner ONCE; it does not build the generic BODY maps/normalization result.
    owner_maps = 1024 + 128 * row_count
    # Every cold appearance carries the actual run ID. The original binding
    # stage grammar bounds nested manifest/constructor names; cold digest
    # labels add owner and occurrence fields. Labels, entry objects, list slots
    # and final tuple slots are all separate original allocations.
    from src.memory.header_bounds import _native_memory_ascii_string_width
    run_width = _native_memory_ascii_string_width(operation.run.run_identity)
    nested_name = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name_width = max(nested_name, len("required-plus-goals-inference-set") + 2,
        4 + len("manifest") + 2 + 20 + len("origin-dependencies") + 2)
    cold_label = (7 + _native_memory_ascii_string_width("native-memory-cold")
        + _native_memory_ascii_string_width("binding-digest") + run_width
        + _native_memory_ascii_string_width("candidate") + 20 + name_width)
    cold_pair = 3 + cold_label + 20
    cold_entry_storage = cold_label + 3 * cold_pair + 2
    cold_outputs = 4 + cold_count * cold_entry_storage
    # Nested cold helpers materialize their original stage vectors before the
    # final cold list/tuple. Binding originals7*60 and five remaps21, candidate
    #33/claim17/original21/reference21/audit6/output2; local payload arrays four
    #times (Original18+4R,Current18+9R+4E). MAC provider source maximum12.
    helper_vectors = (105 + 33 + 17 + 21 + 21 + 6 + 2
        + 72 + 26 * row_count + 8 * event_count
        + 33 * receipt_count + 75 * accepted_count)
    nested_arrays = 4 + helper_vectors * cold_entry_storage
    # Seven original binding60 constructors also allocate23 initial input
    # pairs, two seven-field manifests and six constructor-sizing inputs;
    # price those separately, matching the original Source scalar grammar.
    nested_name = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name_width = max(nested_name, len("required-plus-goals-inference-set") + 2,
        4 + len("manifest") + 2 + 20 + len("origin-dependencies") + 2)
    binding_label = 5 + len("composition-binding-parser") + 2 + run_width + name_width
    binding_pair = 3 + binding_label + 20
    input_pair = 3 + name_width + 20
    binding_storage = (60 * (binding_label + binding_pair) + 35 * nested_name
        + 2 + 2 * (2 + 60 * binding_pair + 59)
        + 23 * input_pair + (2 + 23 * input_pair + 22)
        + 2 * (7 * input_pair + (2 + 7 * input_pair + 6))
        + 6 * input_pair + (2 + 6 * input_pair + 5)
        + (2 + 14 * 20 + 13) + (2 + 34 * 20 + 33)
        + (2 + 6 * 20 + 5))
    nested_arrays += 7 * binding_storage
    # Original Source context planning scalar includes its full120 subtree
    # (and conservatively an extra candidate9), including intermediate parser,
    # admission, candidate and current-digest remap arrays, without building it.
    context_storage = source_owner._original_native_memory_source_schedule_planning_upper(result_present=True)
    scalar_maps = (2 * (2 + 4 * 27) + 4 * (2 + 4 * 27) * (row_count + event_count)
        + receipt_count * (3 * 2 + (25 + 2 + 6) * 27)
        + accepted_count * (2 + 11 * 27))
    # Borrowed-reference tuple, event tuple, literal-key starred tuple and
    # the DISTINCT starred scalar-max generator tuple in the cold constructor.
    incidental_arrays = (2 + 5 * row_count) + (2 + 5 * event_count) + (2 + 21 * row_count)
    incidental_arrays += 2 + 5 * (len(_MEMORY_ORIGINAL_FIELDS) + len(_MEMORY_CURRENT_FIELDS) + 9)
    # Four cold payload traversals call reference() 6R+2E+2 times. Each
    # reference's table-set width traverses the exact retained/Memory roster;
    # include those width generators and original lexical token scans.
    reference_calls = 6 * row_count + 2 * event_count + 2
    reference_controls = reference_calls * (8 + len(RETAINED_FIELDS) + len(MEMORY_DESCRIPTORS))
    controls = 32 * (96 + 8 * row_count + 4 * event_count
        + reference_controls + 2 * lexical_count
        + 16 * (receipt_count + accepted_count))
    # Each added runtime-constructor capacity call has at most three numeric
    # keyword arguments. Pay the forecast's keyword maps separately; these
    # maps are not the future runtime vectors priced by the new14+row pairs.
    controls += (15 + receipt_count + accepted_count) * (3 + 3 * (15 + 2 + 1 + 20))
    upper = (owner_maps + cold_outputs + nested_arrays + context_storage
        + scalar_maps + incidental_arrays + controls)
    if type(upper) is not int or upper < 0:
        raise HeaderBoundsError("memory_cold_construction_numeric_shape_unavailable")
    return upper


def _native_memory_generic_body_construction_numeric_upper(operation, prepared, *,
        certificate, facts, retention_widths):
    """Preconstruction capacity for ONE generic BODY forecast constructor tree.

    Common metadata, Universe, stand-alone cold readers, SourceRows and row
    witnesses are separate. No forecast vector/body/certificate is constructed.
    Nonempty affected cohorts remain unsupported before any allocation.
    """
    from src.runtime_plugins import memory_producer as source_owner
    from src.memory.header_bounds import HeaderBoundsError, MAX_ROWS, MEMORY_DESCRIPTORS
    from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
    source_owner._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard = operation.db.info.get("composition_guard")
    source = operation.source
    proofs = getattr(guard, "_native_memory_retained_proofs", None)
    selected = getattr(guard, "_native_memory_existing_source_rows", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    if (source_owner._MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or type(certificate) is not CompositionHeaderCertificate
            or certificate.seal is not _SEAL or certificate.issued_id != id(certificate)
            or certificate.driver is not operation.driver
            or certificate.metadata_budget is not certificate.budget
            or guard.header_budget is not certificate.budget
            or source_owner._native_memory_source_numeric_frame() is not certificate.budget
            or type(proofs) is not tuple or len(proofs) != 3 or proofs[0] is not operation
            or type(selected) is not tuple or len(selected) != 3 or selected[0] is not operation
            or selected[1] is not certificate or type(projection) is not tuple or len(projection) != 3
            or projection[0] is not operation):
        raise HeaderBoundsError("memory_body_construction_numeric_owner_changed")
    if proofs[2]:
        raise HeaderBoundsError("memory_body_construction_affected_unavailable")
    row_count = len(facts["row_uppers"])
    if (not 1 <= row_count <= MAX_ROWS or retention_widths["row_count"] != row_count
            or type(facts["baseline_trace"]) is not tuple or not facts["baseline_trace"]):
        raise HeaderBoundsError("memory_body_construction_numeric_shape_unavailable")
    # Borrow only original already-paid selected/projection maps. Override
    # lookup uses scalar loops rather than the generic owner's new row map.
    raw = None
    event_count = receipt_count = accepted_count = codec_count = codec_fields = 0
    for descriptor, key, row, encoded in selected[2]:
        if descriptor.table == "workflow_run_states" and key == operation.run.run_identity:
            raw = row["checkpoint_receipts_json"]
    for descriptor, key, changes in projection[2]:
        if (descriptor.table == "workflow_run_states" and key == operation.run.run_identity
                and "checkpoint_receipts_json" in changes):
            raw = changes["checkpoint_receipts_json"]
    for address in facts["row_uppers"]:
        table, key = address
        if table == "work_board_decision_receipts":
            receipt_count += 1
        if type(key) is str:
            fields = NATIVE_EXTENSION_FIELDS.get(table, COMPOSITION_FIELDS.get(table))
            if fields is not None:
                codec_count += 13 if table == "memory_episodes" else 9
                codec_fields += len(fields)
        if table == "memory_proposals":
            status = None
            for descriptor, row_key, row, encoded in selected[2]:
                if descriptor.table == table and row_key == key:
                    status = row["status"]
            for descriptor, row_key, row in projection[1]:
                if descriptor.table == table and row_key == key:
                    status = row["status"]
            for descriptor, row_key, changes in projection[2]:
                if descriptor.table == table and row_key == key and "status" in changes:
                    status = changes["status"]
            if status == "accepted":
                accepted_count += 1
    for descriptor, key, row in projection[1]:
        if descriptor.table in ("audit_events", "work_board_events"):
            event_count += 1
    if type(raw) is not str:
        raise HeaderBoundsError("memory_body_construction_prior_journal_unavailable")
    # Original lexical parser appends one fee pair per strict JSON token. Count
    # the genuine borrowed column without substring/token/list construction.
    position = lexical_count = 0
    while position < len(raw):
        char = raw[position]
        if char in " \t\r\n{}[],:":
            position += 1
            continue
        lexical_count += 1
        if char == '"':
            position += 1
            while position < len(raw):
                if raw[position] == "\\":
                    position += 2
                elif raw[position] == '"':
                    position += 1
                    break
                else:
                    position += 1
        else:
            while position < len(raw) and raw[position] not in " \t\r\n,]}":
                position += 1
    baseline_count = len(facts["baseline_trace"])
    validation_count = 9 if certificate.schema_cookies is not None else 3
    # Cold fixed constructors: candidate33/context120/binding120/digest105/
    # claim17/original21/reference21/audit6/raw1/output2=446. Two journal
    # traversals add94+26R+8E; journal/lexical tail89+17R+4E+L.
    # Receipt21 + one configured MAC<=12; accepted selection51 + two MACs<=24.
    cold_count = (629 + 43 * row_count + 12 * event_count + lexical_count
        + 34 * receipt_count + 76 * accepted_count + 15)
    body_count = (baseline_count + (row_count - 1) * (validation_count + 4)
        + 4 * validation_count + 32 + cold_count)
    # Same immutable-reference4/int64 scalar20 planning convention as the
    # Current constructor. Label <=7 references =>30; pair <=53. Count label,
    # pair, list slot, tuple slot and normalization/maxima dictionary separately.
    owner_maps = 2 * (1024 + 128 * row_count)
    generic_maps = 6 * (2 + 65 * row_count) + 4 * (2 + 27 * body_count)
    body_outputs = 4 + body_count * 150
    # Every cold appearance carries the actual run ID. The original binding
    # stage grammar bounds nested manifest/constructor names; cold digest
    # labels add owner and occurrence fields. Labels, entry objects, list slots
    # and final tuple slots are all separate original allocations.
    from src.memory.header_bounds import _native_memory_ascii_string_width
    run_width = _native_memory_ascii_string_width(operation.run.run_identity)
    nested_name = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name_width = max(nested_name, len("required-plus-goals-inference-set") + 2,
        4 + len("manifest") + 2 + 20 + len("origin-dependencies") + 2)
    cold_label = (7 + _native_memory_ascii_string_width("native-memory-cold")
        + _native_memory_ascii_string_width("binding-digest") + run_width
        + _native_memory_ascii_string_width("candidate") + 20 + name_width)
    cold_pair = 3 + cold_label + 20
    cold_entry_storage = cold_label + 3 * cold_pair + 2
    cold_outputs = 4 + cold_count * cold_entry_storage
    # Codec input9-pair tuple, generated9/13 labels+output, per-field generator
    # storage and each inner dictionary. Audit gets an additional nine-vector.
    codecs = (codec_count + 9) * 96 + row_count * (2 + 9 * 32) + 21 * codec_fields
    # Nested cold helpers materialize their original stage vectors before the
    # final cold list/tuple. Binding originals7*60 and five remaps21, candidate
    #33/claim17/original21/reference21/audit6/output2; local payload arrays four
    #times (Original18+4R,Current18+9R+4E). MAC provider source maximum12.
    helper_vectors = (105 + 33 + 17 + 21 + 21 + 6 + 2
        + 72 + 26 * row_count + 8 * event_count
        + 33 * receipt_count + 75 * accepted_count)
    nested_arrays = 4 + helper_vectors * cold_entry_storage
    # Seven original binding60 constructors also allocate23 initial input
    # pairs, two seven-field manifests and six constructor-sizing inputs;
    # price those separately, matching the original Source scalar grammar.
    nested_name = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name_width = max(nested_name, len("required-plus-goals-inference-set") + 2,
        4 + len("manifest") + 2 + 20 + len("origin-dependencies") + 2)
    binding_label = 5 + len("composition-binding-parser") + 2 + run_width + name_width
    binding_pair = 3 + binding_label + 20
    input_pair = 3 + name_width + 20
    binding_storage = (60 * (binding_label + binding_pair) + 35 * nested_name
        + 2 + 2 * (2 + 60 * binding_pair + 59)
        + 23 * input_pair + (2 + 23 * input_pair + 22)
        + 2 * (7 * input_pair + (2 + 7 * input_pair + 6))
        + 6 * input_pair + (2 + 6 * input_pair + 5)
        + (2 + 14 * 20 + 13) + (2 + 34 * 20 + 33)
        + (2 + 6 * 20 + 5))
    nested_arrays += 7 * binding_storage
    # Original Source context planning scalar includes its full120 subtree
    # (and conservatively an extra candidate9), including intermediate parser,
    # admission, candidate and current-digest remap arrays, without building it.
    context_storage = source_owner._original_native_memory_source_schedule_planning_upper(result_present=True)
    scalar_maps = (2 * (2 + 4 * 27) + 4 * (2 + 4 * 27) * (row_count + event_count)
        + receipt_count * (3 * 2 + (25 + 2 + 6) * 27)
        + accepted_count * (2 + 11 * 27))
    # Borrowed-reference tuple, event tuple, literal-key starred tuple and
    # the DISTINCT starred scalar-max generator tuple in the cold constructor.
    incidental_arrays = (2 + 5 * row_count) + (2 + 5 * event_count) + (2 + 21 * row_count)
    incidental_arrays += 2 + 5 * (len(_MEMORY_ORIGINAL_FIELDS) + len(_MEMORY_CURRENT_FIELDS) + 9)
    # Four cold payload traversals call reference() 6R+2E+2 times. Each
    # reference's table-set width traverses the exact retained/Memory roster;
    # include those width generators, field-pair codec widths, and token scans.
    reference_calls = 6 * row_count + 2 * event_count + 2
    reference_controls = reference_calls * (8 + len(RETAINED_FIELDS) + len(MEMORY_DESCRIPTORS))
    controls = 32 * (96 + 8 * row_count + 4 * event_count + codec_fields
        + reference_controls + 2 * baseline_count + 2 * lexical_count
        + 16 * (receipt_count + accepted_count))
    # Parent retains its distinct five-field lineage AFTER the BODY vector.
    retained_tuple = 2 + 5 * 4
    # Each added runtime-constructor capacity call has at most three numeric
    # keyword arguments. Pay the forecast's keyword maps separately; these
    # maps are not the future runtime vectors priced by the new14+row pairs.
    controls += (15 + receipt_count + accepted_count) * (3 + 3 * (15 + 2 + 1 + 20))
    upper = (owner_maps + generic_maps + body_outputs + cold_outputs + codecs
        + nested_arrays + context_storage + scalar_maps + incidental_arrays + controls + retained_tuple)
    if type(upper) is not int or upper < 0:
        raise HeaderBoundsError("memory_body_construction_numeric_shape_unavailable")
    return upper


def _native_memory_generic_future_numeric_schedule(operation, prepared, *, certificate, facts, retention_widths):
    """ONE supplied-current-certificate BODY snapshot; common certification excluded.

    Retain every real baseline appearance. The outer source owner must add its
    genuine common metadata/certification vector for every invocation before
    issuing a reservation. This vector cannot authorize reads or effects.
    """
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width as width
    from src.workspace.production import _publication_json_upper
    guard, rows, own, audit, new_addresses, value = _native_memory_generic_future_bound_inputs(
        operation, prepared, certificate, facts, retention_widths)
    trace = facts["baseline_trace"]
    if type(trace) is not tuple or not trace:
        raise HeaderBoundsError("memory_future_body_baseline_unavailable")
    validation = None
    validation_names = (("state-changes", "schema-main", "schema-temp")
        if certificate.schema_cookies is not None else ("state-changes",))
    expected_validation = tuple(("metadata-prequery", name, stage)
        for name in validation_names
        for stage in ("delivery", "row-copy", "encoded-copy"))
    validation_count = len(expected_validation)
    for index, (appearance, _upper) in enumerate(trace):
        if appearance == ("body", *own) and index >= validation_count:
            candidate = trace[index - validation_count:index]
            if tuple(item[0] for item in candidate) == expected_validation:
                validation = candidate
                break
    if validation is None:
        raise HeaderBoundsError("memory_future_body_validation_trace_unavailable")
    tail_stages = {appearance[1] for appearance, _upper in trace
        if type(appearance) is tuple and len(appearance) == 2 and appearance[0] == "native-closure-tail"}
    if not {"visited-sort", "visited-return", "table-counts", "role-counts", "relations-sort",
            "relations-json", "relations-utf8", "relations-hash", "result"} <= tail_stages:
        raise HeaderBoundsError("memory_future_body_tail_trace_unavailable")
    projected = dict(facts["row_uppers"])
    projected[own] = retention_widths["own_upper"]
    affected = getattr(guard, "_native_memory_affected_numeric_facts", None)
    affected_widths = {} if not guard._native_memory_retained_proofs[2] else affected[4]
    for run_id, dimensions in affected_widths.items():
        if ("workflow_run_states", run_id) not in facts["baseline_members"]:
            raise HeaderBoundsError("memory_future_body_affected_root_unpaid")
        projected[("workflow_run_states", run_id)] = dimensions["row_upper"]
    added_by_table = {}
    for descriptor, key, _row in guard._native_memory_source_projection[1]:
        if descriptor.table in added_by_table:
            raise HeaderBoundsError("memory_future_body_same_table_insertion_order_unavailable")
        added_by_table[descriptor.table] = key
    def roster(table, original):
        actual = tuple(key for name, key in certificate.rows if name == table)
        if original != actual:
            raise HeaderBoundsError("memory_future_body_original_roster_changed")
        return original + ((added_by_table[table],) if table in added_by_table else ())
    def table_growth(table):
        increase = 0
        for (name, key), upper in projected.items():
            if name != table:
                continue
            old_key = int(key) if name == "work_board_events" and type(key) is str else key
            old = certificate.rows.get((name, old_key))
            increase += upper if old is None else max(0, upper - old[1])
        return increase
    codecs = {address: dict(_original_native_row_digest_entries(address[0], address[1], upper))
        for address, upper in projected.items() if type(address[1]) is str
        and (address[0] in NATIVE_EXTENSION_FIELDS or address[0] in COMPOSITION_FIELDS)}
    cold = _native_memory_postoriginal_cold_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    maxima = {}
    for appearance, upper in cold:
        maxima[appearance] = max(upper, maxima.get(appearance, 0))
    for run_id, dimensions in affected_widths.items():
        for appearance, upper in _native_memory_cold_candidate_numeric_entries(run_id,
                row_upper=dimensions["row_upper"], context_upper=dimensions["context_upper"],
                arguments_upper=dimensions["arguments_upper"], authority_upper=dimensions["authority_upper"]):
            maxima[appearance] = max(upper, maxima.get(appearance, 0))
        from src.runtime_plugins.memory_producer import original_native_memory_source_context_numeric_entries
        # Unknown changes this paid existing root's journal, not its candidate
        # authority. Its whole retained context bounds every nested subvalue.
        source_entries = original_native_memory_source_context_numeric_entries(
            context_upper=dimensions["context_upper"], candidate_upper=dimensions["context_upper"],
            binding_text_bytes=dimensions["binding_text_bytes"], result_upper=dimensions["context_upper"],
            effect_upper=dimensions["context_upper"], locator_upper=dimensions["context_upper"], result_present=True)
        original_appearances = {appearance for appearance, _upper in trace}
        if any(appearance not in original_appearances for appearance, _upper in source_entries):
            raise HeaderBoundsError("memory_future_body_source_trace_unavailable")
        for appearance, upper in source_entries:
            maxima[appearance] = max(upper, maxima.get(appearance, 0))
        # Unknown still parses the same fourteen-field Current record and
        # immutable Original. Its empty rows/events take fewer baseline calls.
        for appearance, upper in (
                (("memory-reference-journal", "decode"), dimensions["journal_upper"]),
                (("memory-reference-journal", "digest-canonical", MEMORY_CURRENT_CHECKPOINT), dimensions["current_upper"]),
                (("memory-reference-journal", "digest-encoded", MEMORY_CURRENT_CHECKPOINT), dimensions["current_upper"]),
                (("memory-reference-payload", "current", "canonical"), dimensions["current_upper"]),
                (("memory-reference-payload", "current", "encoded"), dimensions["current_upper"])):
            maxima[appearance] = max(upper, maxima.get(appearance, 0))
    pair_delta = 3 + width(audit[0]) + width(audit[1]) + 1
    counts_delta = width(audit[0]) + 22
    role = ("audit_event_session_fk", audit[0], audit[1], "sessions", value(audit, "session_id"))
    relation_delta = _publication_json_upper(role) + 1
    role_count_delta = width(role[0]) + 22
    result_delta = counts_delta + role_count_delta + 20
    result = []
    for appearance, upper in trace:
        if type(upper) is not int or not 0 <= upper <= MAX_BYTES:
            raise HeaderBoundsError("memory_future_body_trace_invalid")
        if type(appearance) is tuple:
            if appearance[:1] == ("body",) and len(appearance) == 3:
                upper = max(upper, projected.get(appearance[1:], upper))
            elif appearance[:1] == ("memory-reference-output",) and len(appearance) == 4:
                upper = max(upper, projected.get(appearance[2:], upper))
            elif appearance[:1] == ("native-row-digest",) and len(appearance) >= 4:
                upper = max(upper, codecs.get(appearance[1:3], {}).get(appearance, upper))
            elif appearance[0] in {"table-body", "complete-headers"} and len(appearance) == 3:
                upper += table_growth(appearance[1])
                appearance = (appearance[0], appearance[1], roster(appearance[1], appearance[2]))
            elif appearance[0] == "locator-metadata" and len(appearance) == 5:
                if appearance[1] in added_by_table:
                    # The locator owner must provide its original selected
                    # metadata formula, not a body-byte substitute.
                    raise HeaderBoundsError("memory_future_body_locator_projection_unavailable")
            elif len(appearance) == 2 and appearance[0] == "native-closure-tail":
                if appearance[1] in {"visited-sort", "visited-return"}:
                    upper += pair_delta
                elif appearance[1] == "table-counts":
                    upper += counts_delta
                elif appearance[1] == "role-counts":
                    upper += role_count_delta
                elif appearance[1] in {"relations-sort", "relations-json", "relations-utf8", "relations-hash"}:
                    upper += relation_delta
                elif appearance[1] == "result":
                    upper += result_delta
            elif len(appearance) == 2 and appearance[0] == "programme-snapshot-envelope":
                if appearance[1] in {"construction", "return"}:
                    upper += result_delta
                elif appearance[1] == "members-union":
                    upper += pair_delta
        upper = max(upper, maxima.get(appearance, 0))
        result.append((appearance, upper))
    def body(address, reference=False):
        result.extend(validation)
        result.append((("body", *address), projected[address]))
        if reference:
            result.extend((("memory-reference-output", stage, *address), projected[address])
                for stage in ("mapping", "canonical", "encoded-return"))
    # All Current rows are cold raw reads. Own row uses its original raw codec.
    # Audit has an additional integrity read and generic native-audit read.
    for address in rows:
        if address != own:
            body(address, reference=True)
    body(audit, reference=True)
    body(audit, reference=True)
    # Only the returned native audit becomes newly generic-reachable. Source
    # tasks/attempts and its session were proved in the paid baseline roster.
    body(audit)
    body(audit)
    result.extend(_original_native_row_digest_entries(*audit, projected[audit]))
    result.append((("native-closure-tail", "key-utf8", *audit),
        sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in audit[1])))
    for field, scalar_upper in (("table", len(audit[0])),
            ("key", sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in audit[1])),
            ("leaf", 64)):
        for stage, amount in (("utf8", scalar_upper), ("length-prefix", 8), ("hash-prefix", 8), ("hash-value", scalar_upper)):
            result.append((("native-closure-tail", stage, *audit, field), amount))
    result.extend((appearance, max(upper, maxima[appearance])) for appearance, upper in cold)
    # FIFO queues for a repeated lexical stage must cover whichever literal
    # token reaches that queue first; reserve every actual occurrence, no refund.
    normalized = {}
    for appearance, upper in result:
        normalized[appearance] = max(upper, normalized.get(appearance, 0))
    result = tuple((appearance, max(upper, normalized.get(appearance, upper))) for appearance, upper in result)
    if any(upper > MAX_BYTES for _appearance, upper in result):
        raise HeaderBoundsError("canonical_bound_not_certified")
    return result


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


def _native_memory_source_publication_vector_upper(kind):
    """Encoded storage of the closed <=22-entry numeric vector, no bodies."""
    if kind not in {"capture", "completion", "affected-unknown"}:
        raise ProductionWorkspaceReconciliationError("memory_publication_numeric_shape_unavailable")
    # Four ASCII appearance fields at most, plus one int64 bound per pair.
    # The longest actual label is numeric-consume-tuple (21 ASCII bytes).
    pair = (3 + 20 + 2 + 3 + len("memory-source-sql") + 2 + len(kind) + 2
        + len("consume") + 2 + len("numeric-consume-tuple") + 2)
    return 2 + 21 + 22 * pair


def _native_memory_source_publication_numeric_entries(operation, prepared, *, certificate, retention_widths, kind):
    """Closed SQL owner appearances; numeric lineage is not read permission."""
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_utf8_width
    from src.runtime_plugins.memory_producer import _MEMORY_OPERATIONS, _MEMORY_SOURCES
    if _MEMORY_OPERATIONS.get(id(operation)) is not operation:
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    source = operation.source
    import asyncio
    try:
        task = asyncio.current_task()
    except RuntimeError as error:
        raise HeaderBoundsError("memory_publication_numeric_owner_unavailable") from error
    owner = getattr(operation.db.info.get("composition_guard"), "_native_memory_source_capacity_owner", None)
    effect = getattr(getattr(source, "_retention", None), "ledger", None)
    effect_binding = None if effect is None else effect.bindings.get("effect")
    if (_MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set() or task is None
            or not ((type(owner) is tuple and len(owner) >= 4 and owner[0] is operation and owner[3] is task)
                or (type(effect_binding) is tuple and len(effect_binding) == 6 and effect_binding[5] is task)
                or source._owner_task is task)):
        raise HeaderBoundsError("memory_publication_numeric_owner_unavailable")
    retention = getattr(source, "_retention", None)
    if retention is not None:
        if type(retention) is not _NativeMemoryRetention:
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        retention._checked()
    facts = (getattr(retention, "_publication_numeric_facts", None) if retention is not None
        else getattr(operation.db.info.get("composition_guard"), "_native_memory_retention_numeric_facts", None))
    if (_MEMORY_OPERATIONS.get(id(operation)) is not operation or operation._prepared is not prepared
            or type(retention_widths) is not MappingProxyType or type(facts) is not tuple or len(facts) != 4
            or facts[0] is not operation or facts[1] is not prepared or facts[2] is not certificate
            or kind not in {"capture", "completion", "affected-unknown"}):
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    if kind == "affected-unknown":
        affected = (getattr(retention, "_affected_publication_numeric_facts", None) if retention is not None
            else getattr(operation.db.info.get("composition_guard"), "_native_memory_affected_numeric_facts", None))
        if (type(affected) is not tuple or len(affected) != 5
                or affected[0] is not operation or affected[1] is not prepared or affected[2] is not certificate
                or type(affected[4]) is not MappingProxyType):
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        targets = tuple(key for key, widths in affected[4].items() if widths is retention_widths)
        if len(targets) != 1:
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        proofs = tuple(proof for proof in affected[3] if proof.before['run_identity'] == targets[0])
        if len(proofs) != 1 or proofs[0].operation is not operation:
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        before = proofs[0].before
    elif facts[3] is not retention_widths:
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    elif retention is not None:
        retention._checked()
        before = retention.before
    else:
        selected = operation.db.info["composition_guard"]._native_memory_existing_source_rows
        if selected[0] is not operation:
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        before = next(row for descriptor, key, row in selected[2]
            if descriptor.table == "workflow_run_states" and key == operation.run.run_identity)
    set_names = (("checkpoint_receipts_json", "revision", "updated_at") if kind == "affected-unknown" else ("checkpoint_context_json", "checkpoint_receipts_json", "revision", "updated_at")
        if kind == "capture" else ("status", "result_digest", "result_summary", "failure_reason",
            "checkpoint_context_json", "finished_at", "updated_at", "lease_owner", "lease_expires_at",
            "revision", "checkpoint_receipts_json"))
    where_names = ("run_identity", "status", "revision", "attempt_count", "lease_owner",
        "fencing_token", "checkpoint_receipts_json", "checkpoint_context_json")
    text = retention_widths["completion_future_text_bytes" if kind == "completion" else "future_text_bytes"]
    def old_width(name):
        value = before[name]
        if value is None: return 4
        if type(value) is str: return 6 * _native_memory_utf8_width(value) + 2
        if type(value) is int: return 20
        if type(value) is float and math.isfinite(value): return 64
        raise HeaderBoundsError("memory_publication_bind_unsupported")
    def future_width(name):
        if name in {"updated_at", "finished_at"}: return 194
        if name == "revision": return 20
        if name in {"failure_reason", "lease_owner", "lease_expires_at"}: return 4
        if name in text: return 6 * text[name] + 2
        return old_width(name)
    # UPDATE/SET/WHERE are fixed by the source owner. Identifier quotes and
    # per-predicate parentheses are bounded even when SQLite omits them.
    table = len("workflow_run_states") + 2
    predicates = where_names if kind == "affected-unknown" else (*where_names, "deadline_at", "lease_expires_at")
    sql = (len("UPDATE ") + table + len(" SET ")
        + sum(len(name) + 2 + len("=?") for name in set_names) + 2 * (len(set_names) - 1)
        + len(" WHERE ") + sum(table + 1 + len(name) + 2 + len(" IS NOT NULL") + 2
            for name in predicates) + len(" AND ") * (len(predicates) - 1))
    # At most 21 binds: generated names have column + '_' + <=2 digits.
    values = sum(future_width(name) for name in set_names) + sum(old_width(name) for name in where_names) + (0 if kind == "affected-unknown" else 2 * 194)
    param_keys = sum(len(name) + 5 + 1 for name in (*set_names, *predicates))
    params = 2 + param_keys + values + len(set_names) + len(predicates) - 1
    pending = 2 + sum(len(name) + 3 + future_width(name) for name in set_names) + len(set_names) - 1
    entries = [(('memory-source-sql', kind, 'pending-copy'), pending),
        (('memory-source-sql', kind, 'planned-row'), retention_widths['raw_tuple_upper' if kind == 'affected-unknown' else 'own_raw_upper']),
        (('memory-source-sql', kind, 'conditions'), sql + sum(old_width(name) for name in where_names) + (0 if kind == 'affected-unknown' else 2 * 194)),
        (('memory-source-sql', kind, 'statement'), sql + params)]
    if kind == 'affected-unknown':
        entries = [entry for entry in entries if entry[0][-1] != 'planned-row']
    for stage in ('prepare', 'consume'):
        for appearance, upper in (('compile', sql + params), ('parameters', params),
                ('processed-copy', params), ('sql-text', sql), ('parameter-json', params),
                ('signature', 6 * (sql + params) + 7)):
            entries.append((('memory-source-sql', kind, stage, appearance), upper))
    vector_upper = _native_memory_source_publication_vector_upper(kind)
    for stage in ('plan', 'prepare', 'consume'):
        for copy in ('list', 'tuple'):
            entries.append((('memory-source-sql', kind, 'numeric-' + stage + '-' + copy), vector_upper))
    return tuple(entries)



def _native_memory_engine_publication_numeric_entries(operation, prepared, *, certificate, retention_widths, publication_widths):
    """Original prepare/output allocations, separate from SQL and body traces."""
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError
    from src.runtime_plugins.memory_producer import _MEMORY_OPERATIONS, _MEMORY_SOURCES
    if _MEMORY_OPERATIONS.get(id(operation)) is not operation:
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    source = operation.source
    import asyncio
    try:
        task = asyncio.current_task()
    except RuntimeError as error:
        raise HeaderBoundsError("memory_publication_numeric_owner_unavailable") from error
    owner = getattr(operation.db.info.get("composition_guard"), "_native_memory_source_capacity_owner", None)
    effect = getattr(getattr(source, "_retention", None), "ledger", None)
    effect_binding = None if effect is None else effect.bindings.get("effect")
    if (_MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set() or task is None
            or not ((type(owner) is tuple and len(owner) >= 4 and owner[0] is operation and owner[3] is task)
                or (type(effect_binding) is tuple and len(effect_binding) == 6 and effect_binding[5] is task)
                or source._owner_task is task)):
        raise HeaderBoundsError("memory_publication_numeric_owner_unavailable")
    retention = getattr(operation.source, "_retention", None)
    if retention is not None:
        if type(retention) is not _NativeMemoryRetention:
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        retention._checked()
    facts = (getattr(retention, "_engine_publication_numeric_facts", None) if retention is not None
        else getattr(operation.db.info.get("composition_guard"), "_native_memory_publication_numeric_facts", None))
    if (_MEMORY_OPERATIONS.get(id(operation)) is not operation or operation._prepared is not prepared
            or type(facts) is not tuple or len(facts) != 5
            or facts[0] is not operation or facts[1] is not prepared or facts[2] is not certificate
            or facts[3] is not retention_widths or facts[4] is not publication_widths
            or type(publication_widths) is not MappingProxyType):
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    w = publication_widths
    # Each entry describes one existing appearance in _prepare_publication.
    # Actual row/snapshot/accounting and prior-file reads remain their distinct
    # source traces; this helper never substitutes output width for body fees.
    amounts = (("members-difference", 2 * w["members_bytes"]),
        ("delta-key-union", 2 * w["delta_key_bytes"]),
        ("changed-touched-map", w["changed_touched_bytes"]),
        ("delta-sort", w["changed_touched_bytes"] + 2 * w["delta_key_bytes"]),
        ("delta-construction", w["delta_bytes"]),
        ("checkpoint-construction", w["checkpoint_bytes"]),
        ("receipt-default", w["receipt_default_bytes"]),
        ("receipt-target-update", w["receipt_target_update_bytes"]),
        ("receipt-witness-update", w["receipt_witness_update_bytes"]),
        ("checkpoint-size-json", w["checkpoint_bytes"]),
        ("checkpoint-size-utf8", w["checkpoint_bytes"]),
        ("receipt-size-json", w["receipt_bytes"]),
        ("receipt-size-utf8", w["receipt_bytes"]),
        ("return", w["target_bytes"] + w["checkpoint_bytes"] + w["receipt_bytes"] + 4))
    if any(type(upper) is not int or upper < 0 for _, upper in amounts):
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    entries = [(('composition-prepare-publication', appearance), upper) for appearance, upper in amounts]
    # Original private atomic writers and verification each encode precisely
    # the actual publisher's closed payload, never a fabricated future payload.
    for label, key in (("checkpoint", "checkpoint_bytes"), ("receipt", "receipt_bytes")):
        for appearance in ("dict-copy", "json", "utf8", "readback-dict", "readback-json", "readback-utf8"):
            entries.append((("composition-publication", label, appearance), w[key]))
        entries.append((None, w[key] + 1))  # one original safe readback per output
    return tuple(entries)


def _native_memory_prior_publication_vector_upper():
    """Encoded bound for vector and exact-trace-check list/tuple copies."""
    # 28 output pairs plus 4 checkpoint and 5 receipt grammar-check pairs.
    # Each pair has <=3 ASCII appearance fields and one int64 bound.
    pair = 3 + 20 + 2 + 2 + sum(2 + len(name) for name in
        ("composition-publication", "checkpoint", "materialization"))
    return 2 + 36 + 37 * pair


_NATIVE_MEMORY_ACCOUNTING_TRACES = WeakValueDictionary()
_NATIVE_MEMORY_ACCOUNTING_TRACE_ACTIVE = ContextVar("native_memory_accounting_certification_trace", default=None)


class _NativeMemoryAccountingTraceCollector:
    """Only existing original publication-owner metadata debit observations."""
    def __init__(self, guard, operation, certificate, facts, widths, prior):
        import asyncio
        self.guard, self.operation, self.certificate = guard, operation, certificate
        self.prepared, self.facts, self.widths = operation._prepared, facts, widths
        self.budget, self.task, self.prior = guard.header_budget, asyncio.current_task(), prior
        self.entries = []

    def _checked(self):
        import asyncio
        from src.memory.header_bounds import HeaderBoundsError
        if (_NATIVE_MEMORY_ACCOUNTING_TRACES.get(id(self)) is not self
                or _NATIVE_MEMORY_ACCOUNTING_TRACE_ACTIVE.get() is not self
                or self.guard.db is not self.operation.db
                or self.guard.header_budget is not self.budget
                or self.operation.db.info.get("composition_guard") is not self.guard
                or self.task is not asyncio.current_task()):
            raise HeaderBoundsError("memory_accounting_certification_numeric_owner_changed")
        _native_memory_generic_future_bound_inputs(self.operation, self.prepared,
            self.certificate, self.facts, self.widths)

    def _storage_debit(self, upper, stage):
        from src.memory.header_bounds import _MEMORY_CHARGE_TRACE
        token = _MEMORY_CHARGE_TRACE.set(self.prior)
        try:
            self.budget.debit(upper, appearance=("native-memory-accounting-certification-trace", stage))
        finally:
            _MEMORY_CHARGE_TRACE.reset(token)

    def append_charge(self, appearance, amount):
        from src.memory.header_bounds import HeaderBoundsError
        from src.memory.composition_headers import _common_trace_label_width, _CommonCertificationTraceCollector
        self._checked()
        if type(amount) is not int or not 0 <= amount < 2**63:
            raise HeaderBoundsError("memory_accounting_certification_numeric_shape_changed")
        pair_upper = 3 + _common_trace_label_width(appearance) + 20
        self._storage_debit(pair_upper, "pair")
        self._storage_debit(pair_upper + 1, "list-slot")
        entry = (appearance, amount)
        self.entries.append(entry)
        if self.prior is not None and self.prior[0] is self.budget:
            from src.runtime_plugins.memory_producer import _MemoryAuthorityTraceCollector
            from src.workflows.job_runtime import _NativeMemoryClaimTraceCollector
            outer = self.prior[1]
            if type(outer) in (_CommonCertificationTraceCollector, _MemoryAuthorityTraceCollector,
                    _NativeMemoryClaimTraceCollector, _NativeMemoryAccountingTraceCollector):
                outer.append_charge(appearance, amount)
            else:
                outer.append(entry)


@asynccontextmanager
async def _native_memory_accounting_certification_capture(guard, operation, certificate):
    """Record only the existing three certifiers; no extra query/body read."""
    if operation is None:
        yield
        return
    import asyncio
    from src.memory.header_bounds import _MEMORY_CHARGE_TRACE, HeaderBoundsError
    from src.memory.composition_headers import _common_trace_label_width
    capacity = getattr(guard, "_native_memory_source_capacity_facts", None)
    retained = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if (type(guard) is not CompositionSessionGuard or capacity is None or retained is None
            or capacity[0] is not operation or retained[0] is not operation
            or capacity[1] is not operation._prepared or retained[1] is not operation._prepared
            or capacity[2] is not certificate or retained[2] is not certificate):
        raise HeaderBoundsError("memory_accounting_certification_numeric_owner_changed")
    facts, widths = capacity[3], retained[3]
    _native_memory_generic_future_bound_inputs(operation, operation._prepared, certificate, facts, widths)
    budget = guard.header_budget
    budget.debit(2, appearance=("native-memory-accounting-certification-trace", "empty-list"))
    # Ten owner fields, fixed source ASCII names<=32, opaque pointers4.
    budget.debit(2 + 10 * (6 * 32 + 2 + 4 + 2),
        appearance=("native-memory-accounting-certification-trace", "collector-construction"))
    # Distinct original owner structures are paid before any registration or
    # ContextVar mutation. These match the existing REPORT observer grammar.
    budget.debit(4, appearance=("native-memory-accounting-certification-trace", "collector-weak-reference"))
    budget.debit(27, appearance=("native-memory-accounting-certification-trace", "collector-registry-entry"))
    budget.debit(4, appearance=("native-memory-accounting-certification-trace", "active-context-token"))
    budget.debit(4, appearance=("native-memory-accounting-certification-trace", "trace-context-token"))
    budget.debit(11, appearance=("native-memory-accounting-certification-trace", "trace-binding-tuple"))
    collector = _NativeMemoryAccountingTraceCollector(guard, operation, certificate,
        facts, widths, _MEMORY_CHARGE_TRACE.get())
    _NATIVE_MEMORY_ACCOUNTING_TRACES[id(collector)] = collector
    active_token = _NATIVE_MEMORY_ACCOUNTING_TRACE_ACTIVE.set(collector)
    trace_token = _MEMORY_CHARGE_TRACE.set((budget, collector))
    try:
        collector._checked()
        yield
        collector._checked()
        upper = 2 + max(0, len(collector.entries) - 1)
        for appearance, amount in collector.entries:
            upper += 3 + _common_trace_label_width(appearance) + 20
        collector._storage_debit(upper, "retained-vector")
        collector._storage_debit(upper + 5 * 4 + 7, "retained-lineage")
        guard._native_memory_accounting_certification_numeric = (
            operation, operation._prepared, certificate, budget, asyncio.current_task(),
            tuple(collector.entries))
    finally:
        _MEMORY_CHARGE_TRACE.reset(trace_token)
        _NATIVE_MEMORY_ACCOUNTING_TRACE_ACTIVE.reset(active_token)
        _NATIVE_MEMORY_ACCOUNTING_TRACES.pop(id(collector), None)


def original_native_memory_accounting_certification_numeric_schedule(operation, prepared, *,
        certificate, facts, retention_widths):
    """ONE unchanged accounting locate/certify sequence, numerical reuse only."""
    import asyncio
    from src.memory.header_bounds import HeaderBoundsError
    _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, retention_widths)
    guard = operation.db.info["composition_guard"]
    lineage = getattr(guard, "_native_memory_accounting_certification_numeric", None)
    projection = guard._native_memory_source_projection
    if (type(lineage) is not tuple or len(lineage) != 6
            or lineage[0] is not operation or lineage[1] is not prepared
            or lineage[2] is not certificate or lineage[3] is not certificate.budget
            or lineage[4] is not asyncio.current_task() or type(lineage[5]) is not tuple):
        raise HeaderBoundsError("memory_accounting_certification_numeric_lineage_changed")
    # Sealed M5 projection does not mutate accounting models; no future row or
    # certificate is inferred, and original metadata/query order is preserved.
    for descriptor, key, row in projection[1]:
        if descriptor.table in ("inference_accounting_owners", "inference_cost_reservations"):
            raise HeaderBoundsError("memory_accounting_certification_numeric_projection_changed")
    for descriptor, key, row in projection[2]:
        if descriptor.table in ("inference_accounting_owners", "inference_cost_reservations"):
            raise HeaderBoundsError("memory_accounting_certification_numeric_projection_changed")
    return lineage[5]


def _native_memory_accounting_label_planning_width(value):
    """Scalar label capacity without a recursive sum generator or JSON copy."""
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_utf8_width
    if value is None:
        return 4
    if type(value) is bool:
        return 5
    if type(value) is int:
        return 20
    if type(value) is str:
        return 6 * _native_memory_utf8_width(value) + 2
    if type(value) is tuple:
        result = 2 + max(0, len(value) - 1)
        for item in value:
            result += _native_memory_accounting_label_planning_width(item)
        return result
    raise HeaderBoundsError("memory_accounting_payload_label_unavailable")


def _native_memory_accounting_payload_numeric_entries(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE unchanged accounting body/payload call, excluding prior/cert reads."""
    import asyncio
    from src.runtime_plugins.memory_producer import _native_memory_dispatch_validation_numeric_owner
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_utf8_width
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_accounting_payload_constructor_mode_changed")
    guard = operation.db.info["composition_guard"]
    owner = getattr(guard, "_native_memory_accounting_numeric", None)
    payload = getattr(guard, "_native_memory_accounting_payload_numeric", None)
    common = getattr(guard, "_native_memory_common_allocation_numeric", None)
    if (type(owner) is not tuple or len(owner) != 6
            or owner[0] is not operation or owner[1] is not prepared
            or owner[2] is not certificate or owner[3] is not certificate.budget
            or owner[4] is not asyncio.current_task() or type(owner[5]) is not tuple
            or type(payload) is not tuple or len(payload) != 7
            or payload[0] is not operation or payload[1] is not prepared
            or payload[2] is not certificate or payload[3] is not certificate.budget
            or payload[4] is not asyncio.current_task() or type(payload[5]) is not bool
            or type(payload[6]) is not int or payload[6] < 0
            or type(common) is not tuple or len(common) != 6
            or common[0] is not operation or common[1] is not prepared
            or common[2] is not certificate or common[3] is not facts
            or common[4] is not retention_widths):
        raise HeaderBoundsError("memory_accounting_payload_numeric_lineage_changed")
    projection = guard._native_memory_source_projection
    for component in range(2):
        for descriptor, key, row in projection[component + 1]:
            if descriptor.table == "inference_accounting_owners" or descriptor.table == "inference_cost_reservations":
                raise HeaderBoundsError("memory_accounting_payload_projection_changed")
    account = certificate.rows.get(("inference_accounting_owners", "deployment"))
    if (account is None) != payload[5]:
        raise HeaderBoundsError("memory_accounting_payload_numeric_lineage_changed")
    cost_count = cost_sum = cost_keys_width = 0
    for address in certificate.rows:
        table, key = address
        if table == "inference_cost_reservations":
            cost_count += 1
            cost_sum += certificate.rows[address][1]
            cost_keys_width += 6 * _native_memory_utf8_width(key) + 3
    if payload[5] and (cost_count or owner[5] or payload[6] != 2):
        raise HeaderBoundsError("memory_accounting_payload_numeric_lineage_changed")
    validate_count = 4 if account is None else 5
    validator_entries = 9 if certificate.schema_cookies is not None else 3
    hit = common[5][2][1]
    if len(hit) != validator_entries + 6:
        raise HeaderBoundsError("memory_accounting_payload_validator_shape_changed")
    count = validate_count * validator_entries + len(owner[5]) + 3 + (account is not None)
    # Original labels have <=3 fields. Actual operation IDs in witness labels
    # retain their original widths; no fixed run-ID or label constant replaces
    # them. Count pair objects and both container slots before construction.
    storage = 0
    for component in range(2):
        entries = owner[5] if component == 0 else hit
        limit = len(entries) if component == 0 else validator_entries
        repeats = 1 if component == 0 else validate_count
        for index in range(limit):
            label, amount = entries[index]
            if type(amount) is not int or amount < 0:
                raise HeaderBoundsError("canonical_bound_not_certified")
            label_width = _native_memory_accounting_label_planning_width(label)
            pair_width = 3 + label_width + 20
            storage += repeats * (label_width + 3 * pair_width + 2)
    fixed_label = 4 + len("publication-accounting") + 2 + len("payload-construction") + 2
    body_label = 4 + len("body") + 2 + len("inference_accounting_owners") + 2 + len("deployment") + 2
    table_label = 4 + len("table-body") + 2 + len("inference_cost_reservations") + 2 + 2 + cost_keys_width
    for component in range(3 + (account is not None)):
        label_width = body_label if component == 3 else table_label if component == 0 else fixed_label
        pair_width = 3 + label_width + 20
        storage += label_width + 3 * pair_width + 2
    if _construction_only:
        return 4 + storage + 2 + cost_keys_width + 41 + (count + 8) * 20
    result = []
    for _occurrence in range(validate_count):
        for index in range(validator_entries):
            result.append(hit[index])
    if account is not None:
        result.append((("body", "inference_accounting_owners", "deployment"), account[1]))
    keys = tuple(key for table, key in certificate.rows if table == "inference_cost_reservations")
    result.append((("table-body", "inference_cost_reservations", keys), cost_sum))
    result.append((("publication-accounting", "reservation-list"), 2 + cost_sum + cost_count))
    result.extend(owner[5])
    result.append((("publication-accounting", "empty-payload" if payload[5] else "payload-construction"), payload[6]))
    return tuple(result)


def _native_memory_prior_publication_numeric_entries(operation, prepared, *, certificate,
        retention_widths, publication_widths, prior_reads):
    """Original prior private reads, preserving actual old raw-byte bounds."""
    import asyncio
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError
    from src.runtime_plugins.memory_producer import _MEMORY_OPERATIONS, _MEMORY_SOURCES
    if _MEMORY_OPERATIONS.get(id(operation)) is not operation:
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    source = operation.source
    retention = getattr(source, "_retention", None)
    guard = operation.db.info.get("composition_guard")
    lineage = (getattr(retention, "_prior_publication_numeric", None) if retention is not None
        else getattr(guard, "_native_memory_prior_publication_numeric", None))
    engine = (getattr(retention, "_engine_publication_numeric_facts", None) if retention is not None
        else getattr(guard, "_native_memory_publication_numeric_facts", None))
    try:
        task = asyncio.current_task()
    except RuntimeError as error:
        raise HeaderBoundsError("memory_publication_numeric_owner_unavailable") from error
    if (_MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or operation._prepared is not prepared or task is None
            or type(prior_reads) is not tuple or len(prior_reads) != 7 or lineage is not prior_reads
            or prior_reads[0] is not operation or prior_reads[1] is not prepared
            or prior_reads[2] is not certificate or prior_reads[3] is not source.admission.header_budget
            or prior_reads[4] is not task
            or type(engine) is not tuple or len(engine) != 5
            or engine[0] is not operation or engine[1] is not prepared or engine[2] is not certificate
            or engine[3] is not retention_widths or engine[4] is not publication_widths
            or type(publication_widths) is not MappingProxyType):
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    if retention is not None:
        if type(retention) is not _NativeMemoryRetention:
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        retention._checked()
    def raw_size(trace, label):
        if type(trace) is not tuple:
            raise HeaderBoundsError("memory_publication_private_trace_invalid")
        if not trace: return 0
        if any(type(item) is not tuple or len(item) != 2 or type(item[1]) is not int or item[1] < 0
                for item in trace):
            raise HeaderBoundsError("memory_publication_private_trace_invalid")
        if (type(trace[0]) is not tuple or len(trace[0]) != 2 or trace[0][0] is not None
                or type(trace[0][1]) is not int or trace[0][1] < 1):
            raise HeaderBoundsError("memory_publication_private_trace_invalid")
        size = trace[0][1] - 1
        expected = [(None, size + 1)]
        if label == "receipt":
            expected.append((("composition-publication", label, "decode"), size))
        for appearance in ("json-parse", "materialization", "return"):
            expected.append((("composition-publication", label, appearance), 6 * size + 2))
        if trace != tuple(expected):
            raise HeaderBoundsError("memory_publication_private_trace_invalid")
        return size
    checkpoint = raw_size(prior_reads[5], "checkpoint")
    receipt = raw_size(prior_reads[6], "receipt")
    if any(type(publication_widths[key]) is not int or publication_widths[key] < 0
            for key in ("checkpoint_bytes", "receipt_bytes")):
        raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
    # Capture reads the measured old files; completion can read capture's
    # actual canonical output. Both possibilities are retained, without refund.
    entries = []
    for label, old, key, repeats in (("checkpoint", checkpoint, "checkpoint_bytes", 2),
            ("receipt", receipt, "receipt_bytes", 4)):
        upper = max(old, publication_widths[key])
        for _ in range(repeats):
            entries.append((None, upper + 1))
            if label == "receipt":
                entries.append((("composition-publication", label, "decode"), upper))
            for appearance in ("json-parse", "materialization", "return"):
                entries.append((("composition-publication", label, appearance), 6 * upper + 2))
    return tuple(entries)


def _native_memory_statement_signature(statement, *, native_memory_retention=None, kind=None, publication_widths=None):
    """Bind the original WHERE and the actual SQLite processed parameters."""
    from sqlalchemy.dialects.sqlite import dialect
    from src.workflows.job_runtime import _canonical
    if native_memory_retention is not None:
        import sys
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
        retention = native_memory_retention._checked()
        issuer = sys._getframe(1).f_code
        stage = ("prepare" if issuer is _prepare_native_memory_source_publication.__code__
            else "consume" if issuer is _consume_native_memory_source_publication.__code__ else None)
        active = _MEMORY_LEDGER_PHASE.get()
        if stage is None or active is None or active[0] is not retention.ledger:
            raise ProductionWorkspaceReconciliationError("memory_publication_numeric_owner_unavailable")
        for copy in ('list', 'tuple'):
            retention.source.admission.header_budget.debit(_native_memory_source_publication_vector_upper(kind),
                appearance=('memory-source-sql', kind, 'numeric-' + stage + '-' + copy))
        facts = retention._publication_numeric_facts
        entries = _native_memory_source_publication_numeric_entries(retention.operation, retention.prepared,
            certificate=facts[2], retention_widths=facts[3] if publication_widths is None else publication_widths, kind=kind)
        for appearance, upper in entries:
            if appearance[:3] == ("memory-source-sql", kind, stage):
                retention.source.admission.header_budget.debit(upper, appearance=appearance)
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


@dataclass(eq=False)
class _NativeMemorySourcePublication:
    retention: object
    db: object
    run: object
    statement: object
    before: dict
    after: dict
    certificate: object
    transaction: object
    driver: object
    total_changes: int
    task: object
    signature: tuple
    kind: str
    affected_proof: object = None
    consumed: bool = False
    applied: bool = False
    seal: object = field(default=_MEMORY_PUBLICATION_SEAL, repr=False)


def _native_memory_retention_locator_numeric_entries(operation, prepared, *, certificate,
        facts, retention_widths):
    """ONE actual retention.payload() map and Original digest allocation."""
    from src.memory.header_bounds import _native_memory_object_width as obj, _native_memory_ascii_string_width as width
    _guard, _rows, own, _audit, _new, _value = _native_memory_generic_future_bound_inputs(
        operation, prepared, certificate, facts, retention_widths)
    locator = obj({"profile": width(MEMORY_REFERENCE_PROFILE), "invocation_ref": width(own[1]),
        "original_checkpoint_id": width(MEMORY_ORIGINAL_CHECKPOINT), "original_checkpoint_digest": 66})
    return tuple((("memory-retention-locator", stage), upper) for stage, upper in (
        ("original-canonical", retention_widths["original_record_upper"]),
        ("original-utf8", retention_widths["original_record_upper"]),
        ("original-digest", 66), ("map", locator)))


def _native_memory_retention_validation_numeric_entries(operation, prepared, *, certificate,
        facts, retention_widths):
    """ONE original completion retention lookup, excluding context/body checks."""
    _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, retention_widths)
    return tuple((("memory-retention-validation", stage), upper) for stage, upper in (
        ("registry-view", 4), ("registry-iterator", 4),
        ("matching-count", 20), ("matching-count", 20)))


@dataclass(eq=False)
class _NativeMemoryRetention:
    source: object
    operation: object
    prepared: object
    ledger: object
    before: dict
    new_rows: tuple
    updates: tuple
    existing_references: tuple
    affected: tuple = ()
    future_slots: tuple = ()
    original: object = None
    captured: bool = False
    completed: bool = False
    seal: object = field(default=_MEMORY_PUBLICATION_SEAL, repr=False)

    def _checked(self):
        from src.runtime_plugins.memory_producer import _MEMORY_SOURCES, _MEMORY_OPERATIONS
        if (self.seal is not _MEMORY_PUBLICATION_SEAL
                or _MEMORY_RETENTIONS.get(id(self.source)) is not self
                or getattr(self.source, "_retention", None) is not self
                or _MEMORY_SOURCES.get(id(self.source)) is not self.source
                or not self.source._live.is_set()
                or _MEMORY_OPERATIONS.get(id(self.operation)) is not self.operation
                or self.operation.source is not self.source
                or self.operation._prepared is not self.prepared):
            raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
        self.ledger._checked()
        return self

    @asynccontextmanager
    async def phase(self, phase, db, run):
        from src.runtime_plugins.memory_producer import (
            _validate_memory_owner_source, _validate_memory_lifecycle_source)
        self._checked()
        if phase == "completion":
            if not self.captured or self.completed:
                raise ProductionWorkspaceReconciliationError("memory_retention_completion_unavailable")
        elif phase not in {"effect", "capture"}:
            raise ProductionWorkspaceReconciliationError("memory_retention_phase_invalid")
        await self.ledger.bind(db, phase)
        with self.ledger.phase(phase):
            # Numerical enrollment grants no mutation permission. The original
            # current authority still runs before yielding the effect scope.
            if phase == "completion":
                source = await _validate_memory_lifecycle_source(db, run, self.source.claim)
                if source is not self.source:
                    raise ProductionWorkspaceReconciliationError("memory_retention_source_changed")
            else:
                await _validate_memory_owner_source(db, run, self.operation)
            yield self

    def payload(self):
        from src.workflows.job_runtime import _digest
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, _native_memory_object_width as obj, _native_memory_ascii_string_width as width
        self._checked()
        if not self.captured or self.original is None:
            raise ProductionWorkspaceReconciliationError("memory_retention_original_unavailable")
        active = _MEMORY_LEDGER_PHASE.get()
        if active is None or active[0] is not self.ledger or active[1] not in {"capture", "completion"}:
            raise ProductionWorkspaceReconciliationError("memory_retention_locator_numeric_owner_unavailable")
        _native_memory_capacity_slots(self.ledger)
        retained = getattr(self, "_publication_numeric_facts", None)
        if (retained is None or retained[0] is not self.operation or retained[1] is not self.prepared):
            raise ProductionWorkspaceReconciliationError("memory_retention_locator_numeric_owner_unavailable")
        original_upper = retained[3]["original_record_upper"]
        locator_upper = obj({"profile": width(MEMORY_REFERENCE_PROFILE),
            "invocation_ref": width(self.before["run_identity"]),
            "original_checkpoint_id": width(MEMORY_ORIGINAL_CHECKPOINT), "original_checkpoint_digest": 66})
        for stage, amount in (("original-canonical", original_upper), ("original-utf8", original_upper),
                ("original-digest", 66), ("map", locator_upper)):
            self.ledger.budget.debit(amount, appearance=("memory-retention-locator", stage))
        return {"profile": MEMORY_REFERENCE_PROFILE,
            "invocation_ref": self.before["run_identity"],
            "original_checkpoint_id": MEMORY_ORIGINAL_CHECKPOINT,
            "original_checkpoint_digest": _digest(self.original)}


async def _native_memory_source_snapshot(db, budget):
    from src.memory.universe import native_memory_universe
    from src.memory.composition_headers import _certify_current_memory_snapshot
    await native_memory_universe(db, _header_budget=budget)
    return await _certify_current_memory_snapshot(db, budget, _header_budget=budget)


def _native_memory_future_common_numeric_components(operation, prepared, *, certificate,
        facts, retention_widths, _construction_only=False):
    """Separate full common issuance from its current-cache MISS/HIT wrapper.

    Numerical capacity only: no Memory Universe, accessor validation, future
    certificate or body permission. MISS conservatively includes a stale-cache
    probe even when the future original scope has no cached certificate.
    """
    from src.runtime_plugins import memory_producer as source_owner
    from src.memory.header_bounds import HeaderBoundsError, MAX_ROWS
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_common_construction_mode_unavailable")
    source_owner._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard = operation.db.info.get("composition_guard")
    from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
    source = operation.source
    if (source_owner._MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or type(certificate) is not CompositionHeaderCertificate
            or certificate.seal is not _SEAL or certificate.issued_id != id(certificate)
            or certificate.driver is not operation.driver):
        raise HeaderBoundsError("memory_common_components_numeric_unavailable")
    original = getattr(guard, "_native_memory_source_common_numeric", None)
    snapshot = getattr(guard, "_native_memory_source_snapshot_numeric", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    if (type(original) is not tuple or len(original) != 3
            or original[0] is not operation or original[1] is not certificate
            or type(original[2]) is not tuple
            or type(snapshot) is not tuple or len(snapshot) != 3
            or snapshot[0] is not operation or snapshot[1] is not certificate
            or type(snapshot[2]) is not tuple
            or type(projection) is not tuple or len(projection) != 3
            or projection[0] is not operation
            or guard.header_budget is not certificate.budget
            or source_owner._native_memory_source_numeric_frame() is not certificate.budget
            or certificate.metadata_budget is not certificate.budget
            or certificate.common_certificate is not None or certificate.raw_owner is not None):
        raise HeaderBoundsError("memory_common_components_numeric_unavailable")
    # Original _metadata_scalar(width=20) has the SAME three scalar fees for
    # state-changes and each schema cookie. Authenticate every value against
    # the genuine observed Source snapshot before constructing any vector.
    # _metadata_upper(2, (20,)): outer2 + two one-field rows22 + comma1.
    # Scalar arithmetic avoids constructing the helper generator before payment.
    scalar_upper = 2 + 2 * (2 + 20) + 1
    for name in ("state-changes", "schema-main", "schema-temp"):
        for stage in ("delivery", "row-copy", "encoded-copy"):
            seen = False
            for appearance, amount in snapshot[2]:
                if (type(appearance) is tuple and len(appearance) == 3
                        and appearance[0] == "metadata-prequery"
                        and appearance[1] == name and appearance[2] == stage):
                    if amount != scalar_upper:
                        raise HeaderBoundsError("memory_common_components_numeric_unavailable")
                    seen = True
            if not seen:
                raise HeaderBoundsError("memory_common_components_numeric_unavailable")
    row_count = len(facts["row_uppers"])
    creating = len(projection[1])
    if not 1 <= row_count <= MAX_ROWS or len(certificate.rows) > row_count:
        raise HeaderBoundsError("memory_common_components_numeric_unavailable")
    issuer_count = len(original[2])
    core_count = issuer_count + 5 * creating
    validation_count = 9 if certificate.schema_cookies is not None else 3
    miss_count, hit_count = 2 * validation_count + 12, validation_count + 6
    if _construction_only:
        # Immutable owner references use 4-slot capacity, numbers int64 width20.
        # Existing projected issuer: two generic owner traversals; copied header
        # map, old-id/added/future-id maps (33 descriptors), sorting/slot arrays,
        # result list+tuple/pairs, changed labels with <=128 retained identities.
        # Every issuer item receives the largest source label shape (5 fixed
        # references + one row-id tuple); this overprices unchanged labels.
        owner_storage = 2 * (1024 + 128 * row_count)
        header_storage = 2 + 65 * row_count
        descriptor_maps = 3 * (2 + 33 * 11) + 8 * (2 + 5 * row_count)
        projected_result = 4 + core_count * (37 + 2 + 4 * (5 + row_count))
        # Each new-row header helper constructs its 3*column width list, starred
        # width tuple and three-entry + two-entry + five-entry pair tuples.
        # Actual descriptor column count, never a row/header price substitute.
        row_helpers = 0
        for descriptor, identity, row in projection[1]:
            columns = len(descriptor.columns)
            row_helpers += (2 + 5 * (3 * columns)) + (2 + 21 * (1 + 3 * columns)) + 10 * 32 + 3 * 2
        wrappers = 8 + 43 * (3 * validation_count + 18) + 5 * (5 * validation_count + 36)
        outer = 2 + 3 * 11
        # Source control: 33 per-descriptor old-id generators plus fixed local
        # functions/wrapper generators fit64; two per-issuer generators cover
        # locator/header sums, four per-row cover generic affected/override
        # traversal, and16 per-created-row cover header helper width traversals.
        controls = 32 * (64 + 2 * issuer_count + 4 * row_count + 16 * creating)
        return owner_storage + header_storage + descriptor_maps + projected_result + row_helpers + wrappers + outer + controls
    from src.memory.header_bounds import _native_memory_projected_superset_charges
    core, _headers = _native_memory_projected_superset_charges(certificate, original[2],
        projection[1], projection[2], source_operation=operation)
    if len(core) != core_count:
        raise HeaderBoundsError("memory_common_components_numeric_unavailable")
    def validation():
        return tuple((("metadata-prequery", name, stage), scalar_upper)
            for name in (("state-changes", "schema-main", "schema-temp")
                if certificate.schema_cookies is not None else ("state-changes",))
            for stage in ("delivery", "row-copy", "encoded-copy"))
    def cookies():
        return tuple((("metadata-prequery", name, stage), scalar_upper)
            for name in ("schema-main", "schema-temp")
            for stage in ("delivery", "row-copy", "encoded-copy"))
    # Stale validation, before-issue cookies, after-issue cookies, fresh validate.
    miss = validation() + cookies() + cookies() + validation()
    hit = validation() + cookies()
    return (("core", core), ("miss", miss), ("hit", hit))


def _native_memory_source_snapshot_numeric_entries(operation, prepared, *, certificate,
        facts, retention_widths, _construction_only=False):
    """ONE future original Source snapshot metadata without unused planning storage.

    Body callbacks remain separate. This projects the original full-common
    issuer and retains the actual universe/cache-wrapper appearances. It
    constructs no future certificate, database row or reference permission.
    """
    from src.memory.header_bounds import HeaderBoundsError
    from src.runtime_plugins.memory_producer import _native_memory_dispatch_validation_numeric_owner
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard = operation.db.info["composition_guard"]
    original = getattr(guard, "_native_memory_source_common_numeric", None)
    snapshot = getattr(guard, "_native_memory_source_snapshot_numeric", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    slots = getattr(guard, "_native_memory_integer_slots", None)
    components = getattr(guard, "_native_memory_future_common_components", None)
    if (type(original) is not tuple or len(original) != 3
            or original[0] is not operation or original[1] is not certificate
            or type(original[2]) is not tuple
            or type(snapshot) is not tuple or len(snapshot) != 3
            or snapshot[0] is not operation or snapshot[1] is not certificate
            or type(snapshot[2]) is not tuple
            or type(projection) is not tuple or len(projection) != 3
            or projection[0] is not operation
            or type(slots) is not tuple or len(slots) != 2 or slots[0] is not operation
            or type(_construction_only) is not bool
            or type(components) is not tuple or len(components) != 6
            or components[0] is not operation or components[1] is not prepared
            or components[2] is not certificate or components[3] is not facts
            or components[4] is not retention_widths or type(components[5]) is not tuple):
        raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
    core = None
    for name, entries in components[5]:
        if name == "core":
            if core is not None or type(entries) is not tuple:
                raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
            core = entries
    if core is None:
        raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
    if _construction_only:
        # The wrapper's two index tuples, delivered slices, validation
        # projections, wrapper/cookie slices and final concatenation are
        # numerical containers. Reuse the already constructed common core;
        # no second projector or generic row-map constructor is needed.
        storage, core_storage = 2, 2
        for label, _amount in snapshot[2]:
            storage += 4 + _native_memory_capacity_label_width(label,
                operation=operation, prepared=prepared, certificate=certificate) + 20
        for label, _amount in core:
            core_storage += 4 + _native_memory_capacity_label_width(label,
                operation=operation, prepared=prepared, certificate=certificate) + 20
        # Six generator expressions and three enumerate objects occur in the
        # original wrapper constructor. Preserve their separate conservative
        # 32-slot, int64-width control storage in addition to copied entries.
        return (4 * storage + 2 * core_storage
            + 2 * (2 + 21 * len(snapshot[2])) + 9 * 32 * 20)
    start_label = ("metadata-prequery", "common-certification-trace", "collector-list")
    end_label = ("metadata-prequery", "common-certification-trace", "retained-copy")
    baseline = snapshot[2]
    starts = tuple(index for index, (appearance, _upper) in enumerate(baseline)
        if appearance == start_label)
    ends = tuple(index for index, (appearance, _upper) in enumerate(baseline)
        if appearance == end_label)
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
        # Exact source collector grammar: pair storage, list-slot storage,
        # then the identical original issuer charge, once per ordinal.
        delivered = baseline[starts[0] + 1:ends[0]]
        if len(delivered) != 3 * len(original[2]):
            raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
        for ordinal, source_entry in enumerate(original[2]):
            pair, slot, entry = delivered[3 * ordinal:3 * ordinal + 3]
            if (pair[0] != ("metadata-prequery", "common-certification-trace", "entry-pair", ordinal)
                    or slot[0] != ("metadata-prequery", "common-certification-trace", "entry-list-slot", ordinal)
                    or entry != source_entry):
                raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
        wrapper = baseline[:starts[0]] + baseline[ends[0] + 1:]
    else:
        # A cache hit still has the original genuine issued core supplied by
        # its authenticated accessor. The actual hit validation/cookie costs
        # remain paid; do not pretend that the future issuer costs zero.
        wrapper = baseline
        if certificate.schema_cookies is None:
            # With no certificate cookie fields the actual hit has only one
            # wrapper cookie query pair. A subsequent data-change MISS uses
            # both its before-issuance and after-issuance pair. Preserve the
            # exact original scalar grammar for that second appearance.
            cookies = tuple(entry for entry in baseline
                if type(entry[0]) is tuple and len(entry[0]) == 3
                and entry[0][0] == "metadata-prequery"
                and entry[0][1] in {"schema-main", "schema-temp"})
            expected = tuple(("metadata-prequery", name, stage)
                for name in ("schema-main", "schema-temp")
                for stage in ("delivery", "row-copy", "encoded-copy"))
            if tuple(appearance for appearance, _upper in cookies) != expected:
                raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
            wrapper = wrapper + cookies
    # M5 updates/creates no WRS job identity, job_kind or schema. Thus the
    # original unfiltered native-memory census and fixed schema/cache wrapper
    # are unchanged. The genuine begin snapshot also validates the accessor;
    # this conservatively covers the stale-probe/fresh-issuance validations.
    if not any(appearance == ("metadata-prequery", "memory-universe", "rowid-census", "delivery")
            for appearance, _upper in wrapper):
        raise HeaderBoundsError("memory_source_snapshot_numeric_unavailable")
    # The complete original Memory phase authenticates the issuer's storage-free
    # branch. Its original common/header/schema charges still execute and reach
    # every enclosing observer; only the unused local planning trace is absent.
    # Initial pre-effect issuance above retains and validates its complete trace.
    return tuple(wrapper) + tuple(core)


def _native_memory_source_rows_numeric_entries(operation, prepared, *, certificate,
        facts, retention_widths, _construction_only=False):
    """Source-owned collections, with a scalar constructor cost before building.

    The retained dimension map contains exactly the paid selected addresses
    and sealed new-row capacity addresses. Iterating it needs no prospective
    row map, copied body, certificate or reference permission.
    """
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, MAX_ROWS, _native_memory_ascii_string_width as width
    from src.runtime_plugins.memory_producer import _native_memory_dispatch_validation_numeric_owner
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    if (type(_construction_only) is not bool or type(facts) is not MappingProxyType
            or type(facts.get("row_uppers")) is not MappingProxyType
            or not 1 <= len(facts["row_uppers"]) <= MAX_ROWS):
        raise HeaderBoundsError("memory_source_rows_numeric_shape_unavailable")
    if _construction_only:
        # Ten new two-field labels and amount pairs; two five-pair lists
        # and immutable returns; the two-entry map and its proxy. Stage/upper
        # dispatch reuses literal stage strings and references, not row bodies.
        label = 3 + width("memory-source-rows") + width("reference-tuple")
        pair = 3 + label + 20
        return (10 * label + 10 * pair + 2 * (2 + 5 * pair + 4)
            + 2 * (2 + 5 * pair + 4) + 2 + 1 + 2 + width("all") + width("own")
            + 2 * (2 + 5 * pair + 4) + 4 + 2 * 4)
    all_refs = all_results = 2 + max(0, len(facts["row_uppers"]) - 1)
    own_refs = own_results = None
    for address in facts["row_uppers"]:
        row_upper = facts["row_uppers"][address]
        table, key = address
        key_upper = 22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)
        reference_upper = 3 + width(table) + key_upper
        own = table == "workflow_run_states" and key == operation.run.run_identity
        result_upper = 5 + 4 + key_upper + 2 * (retention_widths["own_upper"] if own else row_upper)
        all_refs += reference_upper
        all_results += result_upper
        if own:
            own_refs, own_results = 2 + reference_upper, 2 + result_upper
    if own_refs is None:
        raise HeaderBoundsError("memory_source_rows_numeric_shape_unavailable")
    all_entries, own_entries = [], []
    for stage in ("reference-set", "reference-sort", "reference-tuple", "result-tuples", "result-return"):
        reference_stage = stage != "result-tuples" and stage != "result-return"
        all_entries.append((("memory-source-rows", stage), all_refs if reference_stage else all_results))
        own_entries.append((("memory-source-rows", stage), own_refs if reference_stage else own_results))
    return MappingProxyType({"all": tuple(all_entries), "own": tuple(own_entries)})


async def _native_memory_source_rows(db, certificate, references):
    from src.memory.composition_headers import snapshot_reads
    from src.memory.header_bounds import _native_memory_ascii_string_width as width
    # Collection capacity uses only this genuine current certificate. It
    # neither selects a new row nor grants a Source body read.
    refs_upper = 2 + max(0, len(references) - 1) + sum(
        3 + width(table) + width(key) for table, key in references)
    for stage in ("reference-set", "reference-sort", "reference-tuple"):
        certificate.budget.debit(refs_upper, appearance=("memory-source-rows", stage))
    references = tuple(sorted(set(references)))
    result_upper = 2 + max(0, len(references) - 1) + sum(
        5 + 4 + width(key) + 2 * certificate.rows[(table,
            int(key) if table == "work_board_events" else key)][1]
        for table, key in references)
    certificate.budget.debit(result_upper, appearance=("memory-source-rows", "result-tuples"))
    def read(connection):
        with snapshot_reads(certificate):
            return tuple((descriptor, key, row, encoded)
                for table, key in references
                for descriptor, row, encoded in (
                    _native_memory_reference_row_on_connection(connection, table, key),))
    result = await (await db.connection()).run_sync(read)
    certificate.budget.debit(result_upper, appearance=("memory-source-rows", "result-return"))
    return result


@dataclass(eq=False)
class _NativeMemoryFutureIntegerSlot:
    """One original plan's numerical liability, never a reference/read grant."""
    operation: object
    prepared: object
    instance: object
    descriptor: object
    db: object
    transaction: object
    driver: object
    task: object
    budget: object
    ordinal: int
    bound_key: object = None
    expected_columns: object = None
    prior_keys: object = None
    seal: object = _MEMORY_PUBLICATION_SEAL

    def __post_init__(self):
        import sys
        from sqlalchemy import inspect
        from src.db.models import WorkBoardEvent
        from src.memory.header_bounds import COMPOSITION_DESCRIPTORS
        if (sys._getframe(2).f_code is not begin_native_memory_effect.__code__
                or self.descriptor is not COMPOSITION_DESCRIPTORS["work_board_events"]
                or type(self.instance) is not WorkBoardEvent
                or not inspect(self.instance).transient or self.instance.event_id is not None
                or self.prepared.consumed or self.prepared._method not in _MEMORY_NATIVE_OPERATIONS
                or any(slot.instance is self.instance for slot in _MEMORY_INTEGER_SLOTS.values())):
            raise ProductionWorkspaceReconciliationError("memory_future_integer_slot_owner_changed")
        _MEMORY_INTEGER_SLOTS[id(self)] = self
        try:
            self._checked()
        except BaseException:
            _MEMORY_INTEGER_SLOTS.pop(id(self), None)
            raise

    def _checked(self):
        import asyncio
        from src.runtime_plugins.memory_producer import _MEMORY_OPERATIONS, _MEMORY_SOURCES
        guard = self.db.info.get("composition_guard")
        if (type(self) is not _NativeMemoryFutureIntegerSlot
                or self.seal is not _MEMORY_PUBLICATION_SEAL
                or _MEMORY_INTEGER_SLOTS.get(id(self)) is not self
                or _MEMORY_OPERATIONS.get(id(self.operation)) is not self.operation
                or _MEMORY_SOURCES.get(id(self.operation.source)) is not self.operation.source
                or not self.operation.source._live.is_set()
                or self.operation._prepared is not self.prepared
                or self.operation.db is not self.db
                or self.operation.transaction is not self.transaction
                or self.operation.driver is not self.driver
                or self.db.sync_session.get_transaction() is not self.transaction
                or asyncio.current_task() is not self.task
                or self.budget is not self.operation.source.admission.header_budget
                or type(guard) is not CompositionSessionGuard or guard.header_budget is not self.budget
                or self.descriptor.table != "work_board_events"
                or not any(row is self.instance for row in self.prepared._rows)):
            raise ProductionWorkspaceReconciliationError("memory_future_integer_slot_changed")
        return self


async def _bind_native_memory_future_integer_slots(db, operation, prepared):
    """Bind only after the genuine flush and original consumed-plan validation."""
    import asyncio
    import sys
    from src.runtime_plugins.memory_producer import perform_memory_mutation
    from src.memory.m5 import validate_native_memory_mutation_plan
    if sys._getframe(1).f_code is not perform_memory_mutation.__code__:
        raise ProductionWorkspaceReconciliationError("memory_future_integer_slot_owner_changed")
    retention = getattr(operation.source, "_retention", None)
    if (type(retention) is not _NativeMemoryRetention or retention.operation is not operation
            or retention.prepared is not prepared or operation.db is not db):
        raise ProductionWorkspaceReconciliationError("memory_future_integer_slot_owner_changed")
    retention._checked()
    if not retention.future_slots:
        return
    await validate_native_memory_mutation_plan(prepared, db=db,
        candidate_digest=operation.source.admission.candidate_digest, consumed=True)
    keys = set()
    bound = []
    for slot in retention.future_slots:
        slot._checked()
        if slot.bound_key is not None:
            raise ProductionWorkspaceReconciliationError("memory_future_integer_slot_already_bound")
        row = _native_memory_planned_sql_row(slot.instance)
        actual_key = row[slot.descriptor.key]
        if type(actual_key) is not int or not -(2**63) <= actual_key < 2**63:
            raise ProductionWorkspaceReconciliationError("memory_future_integer_slot_address_unavailable")
        key = _composition_address(slot.descriptor.table, actual_key)
        if (key in keys or slot.prior_keys is None or key in slot.prior_keys
                or (slot.descriptor.table, key) in retention.existing_references
                or any(row[name] != expected for name, expected in slot.expected_columns.items())
                or slot.task is not asyncio.current_task()):
            raise ProductionWorkspaceReconciliationError("memory_future_integer_slot_changed")
        keys.add(key)
        bound.append((slot, key, row))
    # Publish the numerical bindings only after the whole cohort is checked.
    for slot, key, row in bound:
        slot.bound_key = key
    retention.new_rows = tuple((descriptor, key.bound_key, _native_memory_planned_sql_row(key.instance))
        if type(key) is _NativeMemoryFutureIntegerSlot else (descriptor, key, row)
        for descriptor, key, row in retention.new_rows)


def _native_memory_capacity_slots(ledger):
    """Authenticate a capacity binding without granting a row read."""
    import asyncio
    from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, _NativeMemoryNumericLedger, HeaderBoundsError
    from src.runtime_plugins.memory_producer import _MEMORY_OPERATIONS, _MEMORY_SOURCES
    if type(ledger) is not _NativeMemoryNumericLedger:
        raise HeaderBoundsError("memory_future_integer_slot_changed")
    ledger._checked()
    active = _MEMORY_LEDGER_PHASE.get()
    if active is None or active[0] is not ledger:
        raise HeaderBoundsError("memory_future_integer_slot_changed")
    binding = ledger.bindings.get(active[1])
    if (binding is None or binding[0].sync_session.get_transaction() is not binding[1]
            or not binding[2].in_transaction
            or len(binding) != 6 or binding[5] is not asyncio.current_task()
            or _MEMORY_OPERATIONS.get(id(ledger.operation)) is not ledger.operation
            or _MEMORY_SOURCES.get(id(ledger.source)) is not ledger.source):
        raise HeaderBoundsError("memory_future_integer_slot_changed")
    retention = _MEMORY_RETENTIONS.get(id(ledger.source))
    if (type(retention) is not _NativeMemoryRetention or retention.ledger is not ledger
            or retention.operation is not ledger.operation or retention.prepared is not ledger.prepared
            or ledger.source._retention is not retention):
        raise HeaderBoundsError("memory_future_integer_slot_changed")
    for slot in retention.future_slots:
        if (type(slot) is not _NativeMemoryFutureIntegerSlot
                or _MEMORY_INTEGER_SLOTS.get(id(slot)) is not slot
                or slot.seal is not _MEMORY_PUBLICATION_SEAL
                or slot.operation is not ledger.operation or slot.prepared is not ledger.prepared
                or slot.budget is not ledger.budget
                or not any(row is slot.instance for row in ledger.prepared._rows)):
            raise HeaderBoundsError("memory_future_integer_slot_changed")
    return retention.future_slots


def _native_memory_resolve_numeric_slot(ledger, descriptor, identity, rowid, database_identity):
    """Resolve one paid liability only to its genuine postflush integer key."""
    from sqlalchemy import inspect
    from src.memory.header_bounds import HeaderBoundsError
    retention = _MEMORY_RETENTIONS.get(id(ledger.source))
    if retention is None or not retention.future_slots:
        return None
    slots = _native_memory_capacity_slots(ledger)
    if descriptor.table != "work_board_events" or type(identity) not in {int, str}:
        return None
    address = _composition_address(descriptor.table, identity)
    matches = [slot for slot in slots if slot.descriptor is descriptor and slot.bound_key == address]
    if not matches:
        return None
    if (len(matches) != 1 or database_identity is not None
            or type(rowid) is not int or not -(2**63) <= rowid < 2**63
            or not ledger.prepared.consumed):
        raise HeaderBoundsError("memory_future_integer_slot_changed")
    slot = matches[0]
    value = inspect(slot.instance).attrs[descriptor.key].loaded_value
    if (type(value) is not int or value != rowid
            or _composition_address(descriptor.table, value) != address
            or slot.prior_keys is None or address in slot.prior_keys):
        raise HeaderBoundsError("memory_future_integer_slot_changed")
    return slot


def _native_memory_normalize_numeric_appearance(ledger, appearance):
    """Alias only original address-bearing fees to their prepaid capacity slot."""
    if (type(appearance) is not tuple or not appearance or appearance[0] not in {
            "body", "memory-reference-output", "native-row-digest", "memory-row-witness",
            "native-closure-tail", "complete-headers", "table-body", "locator-metadata"}):
        # Original completion startup can consume numerical metadata before its
        # fresh writer exists. Those fees contain no physical address to bind.
        return appearance
    if ((appearance[0] == "native-closure-tail" and len(appearance) not in {4, 5})
            or (appearance[0] == "native-row-digest" and len(appearance) not in {4, 5})
            or (appearance[0] in {"body", "complete-headers", "table-body"} and len(appearance) != 3)
            or (appearance[0] in {"memory-reference-output", "memory-row-witness"} and len(appearance) != 4)
            or (appearance[0] == "locator-metadata" and len(appearance) != 5)):
        return appearance
    retention = _MEMORY_RETENTIONS.get(id(ledger.source))
    if retention is None or not retention.future_slots:
        return appearance
    slots = _native_memory_capacity_slots(ledger)
    if not slots or type(appearance) is not tuple or not appearance:
        return appearance
    def address(table, key):
        selected_key = (_composition_address(table, key)
            if table == "work_board_events" and type(key) in {int, str} else key)
        matched = [slot for slot in slots if slot.descriptor.table == table
            and slot.bound_key is not None and slot.bound_key == selected_key]
        if len(matched) > 1:
            from src.memory.header_bounds import HeaderBoundsError
            raise HeaderBoundsError("memory_future_integer_slot_changed")
        return matched[0] if matched else key
    if appearance[0] == "body" and len(appearance) == 3:
        return (appearance[0], appearance[1], address(appearance[1], appearance[2]))
    if appearance[0] == "memory-reference-output" and len(appearance) == 4:
        return (*appearance[:3], address(appearance[2], appearance[3]))
    if appearance[0] == "native-row-digest" and len(appearance) in {4, 5}:
        return (appearance[0], appearance[1], address(appearance[1], appearance[2]), *appearance[3:])
    if appearance[0] == "memory-row-witness" and len(appearance) == 4:
        return (appearance[0], appearance[1], address(appearance[1], appearance[2]), appearance[3])
    if appearance[0] == "native-closure-tail" and len(appearance) in {4, 5}:
        return (*appearance[:3], address(appearance[2], appearance[3]), *appearance[4:])
    if appearance[0] in {"complete-headers", "table-body"} and len(appearance) == 3:
        return (appearance[0], appearance[1], tuple(address(appearance[1], key) for key in appearance[2]))
    if appearance[0] == "locator-metadata" and len(appearance) == 5:
        return (appearance[0], appearance[1], address(appearance[1], appearance[2]),
            appearance[3], tuple(address(appearance[1], key) for key in appearance[4]))
    # An arbitrary integer/string nested in a fee is never an address alias.
    return appearance


@dataclass(eq=False)
class _NativeMemoryRetainedCurrentProof:
    operation: object
    guard: object
    connection: object
    task: object
    before: dict
    original: dict
    current: dict
    seal: object = _MEMORY_PUBLICATION_SEAL


def _record_native_memory_retained_current(connection, row, original, current):
    """Retain successful accounting evidence without restoring an old Source."""
    import asyncio
    import sys
    from copy import deepcopy
    from src.memory.composition_headers import _CURRENT
    from src.runtime_plugins.memory_producer import _MEMORY_OPERATIONS
    collected = _MEMORY_RETENTION_PROOFS.get()
    if collected is None:
        return
    operation, guard, task, proofs = collected
    certificate = _CURRENT.get()
    if (sys._getframe(1).f_code is not _checked_memory_reference_row.__code__
            or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or asyncio.current_task() is not task or operation.db is not guard.db
            or guard.db.info.get("composition_guard") is not guard
            or certificate is None or certificate.connection is not connection
            or certificate.budget is not operation.source.admission.header_budget
            or certificate.budget is not guard.header_budget
            or operation._effect is not None or original is None
            or current["payload"]["state"] != "validated"):
        raise ProductionWorkspaceReconciliationError("memory_retained_current_owner_changed")
    upper = certificate.rows[("workflow_run_states", row["run_identity"])][1]
    for stage, amount in (("raw-copy", upper),
            ("original-copy", _native_memory_closed_value_width(original)),
            ("current-copy", _native_memory_closed_value_width(current))):
        certificate.budget.debit(amount,
            appearance=("memory-retained-current", stage, row["run_identity"]))
    proofs.append(_NativeMemoryRetainedCurrentProof(operation, guard, connection, task,
        dict(row), deepcopy(original), deepcopy(current)))


async def _native_memory_retained_current_roster(db, run, operation, certificate):
    """Pay the existing full accounting readset; do not add a foreign-body scan."""
    import asyncio
    from src.runtime_plugins.memory_producer import _validate_memory_owner_source
    from src.memory.header_bounds import _trace_memory_numeric_charges
    source = await _validate_memory_owner_source(db, run, operation)
    guard = db.info.get("composition_guard")
    if (type(guard) is not CompositionSessionGuard or guard.db is not db
            or guard.header_budget is not source.admission.header_budget
            or certificate.budget is not guard.header_budget
            or _MEMORY_RETENTION_PROOFS.get() is not None):
        raise ProductionWorkspaceReconciliationError("memory_retained_current_owner_changed")
    connection = await db.connection()
    proofs = []
    token = _MEMORY_RETENTION_PROOFS.set((operation, guard, asyncio.current_task(), proofs))
    try:
        with _trace_memory_numeric_charges(guard.header_budget) as trace:
            baseline, members = await connection.run_sync(
                lambda current: guard._programme_snapshot(current, certificate, guard.base))
    finally:
        _MEMORY_RETENTION_PROOFS.reset(token)
    guard._native_memory_baseline_numeric = (operation, certificate, tuple(trace))
    guard._native_memory_baseline_numeric_members = (operation, frozenset(members))
    changed = {(instance.__tablename__, _composition_address(instance.__tablename__, key))
        for instance, key, _changes in operation._prepared.original_update_rows()}
    affected = tuple(proof for proof in proofs if any(
        (ref["table"], ref["key"]) in changed for ref in proof.current["payload"]["refs"]))
    guard._native_memory_retained_proofs = (operation, tuple(proofs), affected)
    return affected


async def _native_memory_source_capacity_facts(db, run, operation, prepared, certificate):
    """Retain numeric dimensions of the paid Source roster and sealed binds.

    The keys are capacity addresses, including private unbound integer slots.
    This record is not a prospective certificate or a read permission.
    """
    import asyncio
    from types import MappingProxyType
    from src.runtime_plugins.memory_producer import _validate_memory_owner_source
    from src.memory.composition_headers import validate_composition_certificate
    from src.memory.header_bounds import (
        COMPOSITION_DESCRIPTORS, HeaderBoundsError, MAX_ROWS,
        _native_memory_resolved_row_widths, _native_memory_integer_slot_row_widths,
        _native_memory_utf8_width, _native_memory_ascii_string_width)
    source = await _validate_memory_owner_source(db, run, operation)
    guard = db.info.get("composition_guard")
    selected = getattr(guard, "_native_memory_existing_source_rows", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    baseline = getattr(guard, "_native_memory_baseline_numeric", None)
    members = getattr(guard, "_native_memory_baseline_numeric_members", None)
    if (type(guard) is not CompositionSessionGuard or guard.db is not db
            or guard.header_budget is not source.admission.header_budget
            or certificate.budget is not guard.header_budget
            or operation._prepared is not prepared or prepared.consumed
            or operation._effect is not None
            or selected is None or selected[0] is not operation
            or projection is None or projection[0] is not operation
            or baseline is None or baseline[0] is not operation or baseline[1] is not certificate
            or members is None or members[0] is not operation):
        raise HeaderBoundsError("memory_source_numeric_facts_changed")
    await validate_composition_certificate(db, certificate)
    selected_rows = selected[2]
    new_rows, updates = projection[1:]
    if len(selected_rows) + len(new_rows) > MAX_ROWS:
        raise HeaderBoundsError("composition_row_bound")
    # Pay these numeric maps and the temporary merged bind maps before copying.
    # Actual row bodies were delivered by the original selected read above.
    def numeric_row_width(descriptor, key):
        address_upper = (3 + _native_memory_ascii_string_width(descriptor.table)
            + (22 if type(key) is _NativeMemoryFutureIntegerSlot
               else _native_memory_ascii_string_width(key)))
        text_columns = sum(kind == "text" for kind in descriptor.kinds)
        text_map_upper = (2 + max(0, text_columns - 1) + sum(
            _native_memory_ascii_string_width(name) + 1 + 7
            for name, kind in zip(descriptor.columns, descriptor.kinds) if kind == "text"))
        # Three dimension-map keys and one selected-address entry. All width
        # values are bounded by the unchanged seven-digit MAX_BYTES count.
        return 4 * address_upper + 7 + 7 + text_map_upper + 4
    numeric_maps_upper = (2 + sum(_native_memory_ascii_string_width(name) + 1
        for name in ("row_uppers", "raw_tuple_uppers", "text_bytes",
            "selected_addresses", "baseline_members", "baseline_trace"))
        + sum(numeric_row_width(descriptor, key)
        for descriptor, key, _row, _encoded in selected_rows)
        + sum(numeric_row_width(descriptor, key) for descriptor, key, _row in new_rows))
    guard.header_budget.debit(numeric_maps_upper,
        appearance=("memory-source-capacity", "numeric-maps", run.run_identity))
    headers, tuples, texts = {}, {}, {}
    paid_addresses = frozenset((descriptor.table, key)
        for descriptor, key, _row, _encoded in selected_rows)
    if any((descriptor.table, key) not in paid_addresses for descriptor, key, _changes in updates):
        raise HeaderBoundsError("memory_source_projection_row_unpaid")
    for descriptor, key, row, _encoded in selected_rows:
        changes = next((values for changed, address, values in updates
            if changed is descriptor and address == key), None)
        if changes is not None:
            guard.header_budget.debit(certificate.rows[(descriptor.table,
                int(key) if descriptor.table == "work_board_events" else key)][1],
                appearance=("memory-source-capacity", "merged-bind-copy", descriptor.table, key))
            row = dict(row, **changes)
        upper, raw_upper = _native_memory_resolved_row_widths(descriptor, key, row)
        address = (descriptor.table, key)
        headers[address], tuples[address] = upper, raw_upper
        texts[address] = MappingProxyType({name: _native_memory_utf8_width(row[name])
            for name, kind in zip(descriptor.columns, descriptor.kinds)
            if kind == "text" and row[name] is not None})
    for descriptor, key, row in new_rows:
        address = (descriptor.table, key)
        if address in headers or descriptor is not COMPOSITION_DESCRIPTORS[descriptor.table]:
            raise HeaderBoundsError("memory_source_projection_row_collision")
        upper, raw_upper = (_native_memory_integer_slot_row_widths(key, row)
            if type(key) is _NativeMemoryFutureIntegerSlot
            else _native_memory_resolved_row_widths(descriptor, key, row))
        headers[address], tuples[address] = upper, raw_upper
        texts[address] = MappingProxyType({name: _native_memory_utf8_width(row[name])
            for name, kind in zip(descriptor.columns, descriptor.kinds)
            if kind == "text" and row[name] is not None})
    facts = MappingProxyType({"row_uppers": MappingProxyType(headers),
        "raw_tuple_uppers": MappingProxyType(tuples), "text_bytes": MappingProxyType(texts),
        "selected_addresses": paid_addresses, "baseline_members": members[1],
        "baseline_trace": baseline[2]})
    guard._native_memory_source_capacity_facts = (operation, prepared, certificate, facts)
    guard._native_memory_source_capacity_owner = (
        operation, prepared, certificate, asyncio.current_task(),
        db.sync_session.get_transaction(), operation.driver)
    return facts


def _native_memory_source_retention_widths(operation, prepared, before, certificate, facts):
    """Closed Original/Current dimensions of a successful sealed owner plan.

    Only numeric widths are constructed. In particular, an unbound event slot
    never becomes a reference, receipt, row value or certificate here.
    """
    import asyncio
    from types import MappingProxyType
    from src.memory.header_bounds import (
        HeaderBoundsError, MAX_BYTES, _native_memory_ascii_string_width,
        _native_memory_utf8_width, _native_memory_object_width,
        _native_memory_array_width, _native_memory_future_wrs_widths)
    from src.runtime_plugins.contracts import MEMORY_STATUS
    from src.runtime_plugins.memory_producer import _MemoryOwnerSourceOperation, _MEMORY_OPERATIONS
    from src.memory.m5 import PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS
    if (type(operation) is not _MemoryOwnerSourceOperation
            or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or type(prepared) is not PreparedNativeMemoryMutation
            or _NATIVE_MUTATION_PLANS.get(id(prepared)) is not prepared):
        raise HeaderBoundsError("memory_source_numeric_facts_changed")
    guard = operation.db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_source_capacity_facts", None)
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    if (type(guard) is not CompositionSessionGuard or retained is None
            or owner is None or owner[0] is not operation or owner[1] is not prepared
            or owner[2] is not certificate or owner[3] is not asyncio.current_task()
            or owner[4] is not operation.db.sync_session.get_transaction()
            or owner[4] is not operation.transaction or owner[5] is not operation.driver
            or not operation.driver.in_transaction
            or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not facts
            or prepared.consumed or operation._effect is not None
            or operation._prepared is not prepared
            or before["run_identity"] != operation.run.run_identity
            or not any(descriptor.table == "workflow_run_states" and key == before["run_identity"]
                and row == before for descriptor, key, row, _encoded
                in guard._native_memory_existing_source_rows[2])):
        raise HeaderBoundsError("memory_source_numeric_facts_changed")
    if type(before["revision"]) is not int or not -(2**63) <= before["revision"] <= 2**63 - 3:
        raise HeaderBoundsError("memory_source_completion_revision_unavailable")
    width, obj, array = (_native_memory_ascii_string_width,
        _native_memory_object_width, _native_memory_array_width)
    method, result = prepared._method, prepared._result
    if method == "memory.propose" and result.get("status") == "proposed":
        proposal = result["proposal_id"]
        record = None
        value_upper = obj({"proposal_ref": width(proposal), "revision": 20, "state": width("proposed")})
    elif (method == "memory.applyReviewed" and result.get("status") == "accepted"
            and not result.get("idempotent_replay")):
        proposal, record = result["proposal_id"], result["accepted_memory_id"]
        value_upper = obj({"record_ref": width(record)})
    elif method == "memory.forget":
        proposal, record = None, result["memory"]["id"]
        value_upper = obj({"record_ref": width(record)})
    else:
        raise HeaderBoundsError("memory_source_retention_outcome_unavailable")
    audit = (prepared.audit_event.id if prepared.audit_event is not None
        else result.get("audit_event_id"))
    if type(audit) is not str or ("audit_events", audit) not in facts["row_uppers"]:
        raise HeaderBoundsError("memory_source_retention_audit_unavailable")
    addresses = tuple(facts["row_uppers"])
    refs = tuple(obj({"table": width(table), "key": (
        22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key))})
        for table, key in addresses)
    refs_upper = array(refs)
    rows_upper = array(tuple(obj({"ref": ref, "tuple_digest": 66, "encoded_bytes": 7}) for ref in refs))
    event_widths = tuple(ref for (table, _key), ref in zip(addresses, refs)
        if table in {"audit_events", "work_board_events"}
        and any(descriptor.table == table and (key is _key or key == _key)
            for descriptor, key, _row in guard._native_memory_source_projection[1]))
    if len(event_widths) > 2:
        raise HeaderBoundsError("memory_source_retention_events_unavailable")
    events_upper = array(event_widths)
    principal = operation.source.operator.principal.principal_id
    session = operation.source.operator.session_id
    original_fields = {"schema_version": 1, "profile": width(MEMORY_REFERENCE_PROFILE),
        "invocation_ref": width(before["run_identity"]), "claim_ref": width(operation.source._scope.witness["claim_ref"]),
        "candidate_digest": 66, "composition_binding_digest": 66, "method": width(method),
        "owner_principal_id": width(principal), "operator_session_id": width(session),
        "original_deadline": 34, "source_binding_digest": 66, "original_effect_digest": 66,
        "original_audit": obj({"ref": obj({"table": width("audit_events"), "key": width(audit)}), "tuple_digest": 66}),
        "refs": refs_upper, "rows_digest": 66, "encoded_bytes": 7}
    current_fields = {"schema_version": 1, "profile": width(MEMORY_REFERENCE_PROFILE),
        "original_checkpoint_digest": 66, "projection_revision": 20, "state": width("validated"),
        "reason_code": 4, "refs": refs_upper, "absences": 2, "rows": rows_upper,
        "rows_digest": 66, "encoded_bytes": 7, "owner_operation_kind": width(method),
        "owner_events": events_upper, "selected_delta_digest": 66}
    if original_fields.keys() != _MEMORY_ORIGINAL_FIELDS or current_fields.keys() != _MEMORY_CURRENT_FIELDS:
        raise HeaderBoundsError("memory_source_retention_shape_changed")
    original_upper, current_upper = obj(original_fields), obj(current_fields)
    original_record = _native_memory_reference_record_upper(original_upper, current=False)
    current_record = _native_memory_reference_record_upper(current_upper, current=True)
    effect_upper = obj({"method": width(method), "candidate_digest": 66, "status": width("succeeded"),
        "value_json": 6 * value_upper + 2, "audit_event_id": width(audit),
        "proposal_id": 4 if proposal is None else width(proposal), "record_id": 4 if record is None else width(record)})
    protocol_value = value_upper
    if method != "memory.propose":
        # Original returned value adds the fixed prefix and SHA locator.
        protocol_value += width("receipt_ref") + 1 + width("native-memory-receipt:") + 64 + 1
    result_upper = obj({"status": width("succeeded"), "value": protocol_value,
        "memory_status": width(MEMORY_STATUS[method])})
    locator_upper = obj({"profile": width(MEMORY_REFERENCE_PROFILE),
        "invocation_ref": width(before["run_identity"]), "original_checkpoint_id": width(MEMORY_ORIGINAL_CHECKPOINT),
        "original_checkpoint_digest": 66})
    sealed_upper = obj({"invocation_ref": width(before["run_identity"]),
        "claim_ref": width(operation.source._scope.witness["claim_ref"]), "candidate_digest": 66,
        "result_digest": 66, "payload": result_upper, "effect": effect_upper, "retention": locator_upper})
    future_context = _native_memory_utf8_width(before["checkpoint_context_json"]) + sealed_upper
    future_journal = (_native_memory_utf8_width(before["checkpoint_receipts_json"])
        + original_record + current_record + 2)
    future_text_values = {"checkpoint_context_json": future_context,
        "checkpoint_receipts_json": future_journal, "updated_at": 32}
    own_upper, own_raw_upper = _native_memory_future_wrs_widths(before,
        text_bytes=future_text_values, integer_fields=("revision",))
    completion_text_values = dict(future_text_values,
        status=len("succeeded"), result_digest=64,
        result_summary=len("Native Memory owner result committed"), finished_at=32)
    completion_upper, completion_raw_upper = _native_memory_future_wrs_widths(before,
        text_bytes=completion_text_values,
        null_fields=("lease_owner", "lease_expires_at", "failure_reason"),
        integer_fields=("revision",))
    # The identical reserved owner continues through original completion.
    # Its additional fixed result/time columns must fit before the first effect.
    own_upper = max(own_upper, completion_upper)
    own_raw_upper = max(own_raw_upper, completion_raw_upper)
    future_text = MappingProxyType(future_text_values)
    if max(original_upper, current_upper, sealed_upper, future_context, future_journal) > MAX_BYTES:
        raise HeaderBoundsError("canonical_bound_not_certified")
    own_address = ("workflow_run_states", before["run_identity"])
    own_text = facts["text_bytes"][own_address]
    audit_address = ("audit_events", audit)
    selected_rows_upper = (sum(facts["row_uppers"].values())
        - facts["row_uppers"][own_address] + own_upper)
    return MappingProxyType({"effect_upper": effect_upper, "result_upper": result_upper,
        "original_upper": original_upper, "original_record_upper": original_record,
        "current_upper": current_upper, "current_record_upper": current_record,
        "rows_upper": rows_upper, "row_count": len(addresses), "refs_upper": refs_upper,
        "row_upper": own_upper, "own_upper": own_upper, "own_raw_upper": own_raw_upper,
        "selected_rows_upper": selected_rows_upper,
        "own_journal_upper": 6 * future_journal + 2, "context_upper": 6 * future_context + 2,
        "journal_upper": 6 * future_journal + 2, "claimed": True,
        "arguments_upper": 6 * own_text["arguments_json"] + 2,
        "authority_upper": 6 * own_text["declared_authority_json"] + 2,
        "source_upper": 6 * _native_memory_utf8_width(operation.source.admission.candidate_json) + 2,
        "binding_text_bytes": own_text["composition_binding_json"],
        "audit_upper": facts["row_uppers"][audit_address],
        "details_upper": 6 * facts["text_bytes"][audit_address]["details_json"] + 2,
        "future_text_bytes": future_text,
        "completion_future_text_bytes": MappingProxyType(completion_text_values)})


def _native_memory_source_affected_widths(operation, prepared, certificate, proofs):
    """Numeric Unknown liabilities for exactly the paid affected proof cohort."""
    import asyncio
    from types import MappingProxyType
    from src.memory.header_bounds import (HeaderBoundsError, _native_memory_current_payload_upper,
        _native_memory_future_wrs_widths, _native_memory_utf8_width,
        _native_memory_ascii_string_width, _native_memory_object_width)
    guard = operation.db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_retained_proofs", None)
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    if (type(guard) is not CompositionSessionGuard or retained is None
            or retained[0] is not operation or retained[2] is not proofs
            or owner is None or owner[0] is not operation or owner[1] is not prepared
            or owner[2] is not certificate or owner[3] is not asyncio.current_task()
            or owner[4] is not operation.db.sync_session.get_transaction()
            or owner[5] is not operation.driver or not operation.driver.in_transaction
            or prepared.consumed or operation._effect is not None):
        raise HeaderBoundsError("memory_affected_numeric_facts_changed")
    frame = certificate.budget
    numeric = {}
    footprint = {(descriptor.table, key) for descriptor, key, _changes in guard._native_memory_source_projection[2]}
    for proof in proofs:
        if (type(proof) is not _NativeMemoryRetainedCurrentProof or proof.operation is not operation
                or proof.guard is not guard or proof.task is not asyncio.current_task()
                or proof.seal is not _MEMORY_PUBLICATION_SEAL
                or not any((ref["table"], ref["key"]) in footprint for ref in proof.current["payload"]["refs"])):
            raise HeaderBoundsError("memory_affected_numeric_facts_changed")
        row = proof.before
        target = row["run_identity"]
        if target in numeric or ("workflow_run_states", target) not in certificate.rows:
            raise HeaderBoundsError("memory_affected_numeric_facts_changed")
        if row["revision"] == 2**63 - 1 or proof.current["payload"]["projection_revision"] == 2**63 - 1:
            raise HeaderBoundsError("memory_affected_numeric_revision_unavailable")
        refs_upper = _native_memory_closed_value_width(proof.current["payload"]["refs"])
        frame.debit(refs_upper, appearance=("memory-source-capacity", "affected-reference-tuples", target))
        refs = tuple((ref["table"], ref["key"]) for ref in proof.current["payload"]["refs"])
        payload, record = _native_memory_current_payload_upper(refs, (), method=prepared._method,
            unknown_reason="current_source_unavailable")
        journal_bytes = _native_memory_utf8_width(row["checkpoint_receipts_json"]) + record + 1
        future_text = {"checkpoint_receipts_json": journal_bytes, "updated_at": 32}
        header, raw = _native_memory_future_wrs_widths(row, text_bytes=future_text, integer_fields=("revision",))
        frame.debit(2 + sum(_native_memory_ascii_string_width(name) + 1 + 7 + 1
            for name in ("row_upper", "raw_tuple_upper", "journal_upper", "context_upper",
                "arguments_upper", "authority_upper", "binding_text_bytes", "original_upper",
                "current_upper", "current_record_upper", "refs_upper"))
            + _native_memory_ascii_string_width(target) + 1,
            appearance=("memory-source-capacity", "affected-numeric-map", target))
        fields = {"row_upper": header, "raw_tuple_upper": raw,
            "journal_upper": 6 * journal_bytes + 2,
            "context_upper": 6 * _native_memory_utf8_width(row["checkpoint_context_json"]) + 2,
            "arguments_upper": 6 * _native_memory_utf8_width(row["arguments_json"]) + 2,
            "authority_upper": 6 * _native_memory_utf8_width(row["declared_authority_json"]) + 2,
            "binding_text_bytes": _native_memory_utf8_width(row["composition_binding_json"]),
            "original_upper": _native_memory_closed_value_width(proof.original),
            "current_upper": payload, "current_record_upper": record, "refs_upper": refs_upper}
        numeric[target] = MappingProxyType(dict(fields, future_text_bytes=MappingProxyType(future_text)))
    result = MappingProxyType(numeric)
    guard._native_memory_affected_numeric_facts = (operation, prepared, certificate, proofs, result)
    return result


def _native_memory_minimal_call_vector(operation, prepared, *, certificate, facts,
        retention_widths):
    """Literal owner-call roster for the sealed successful propose path.

    Common acquisition, body traversal and cold readback are distinct calls.
    The first-write programme check is conservatively retained in effect,
    where the sealed M5 plan first writes. Once that check has run, capture
    reuses the same writer; completion starts its own original writer.
    An affected retained root requires its own whole Unknown path, not this
    one-root roster.
    """
    from src.runtime_plugins.memory_producer import _native_memory_dispatch_validation_numeric_owner
    from src.memory.header_bounds import HeaderBoundsError
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    proofs = getattr(operation.db.info["composition_guard"], "_native_memory_retained_proofs", None)
    if (prepared._method != "memory.propose" or prepared._result.get("status") != "proposed"
            or type(proofs) is not tuple or len(proofs) != 3
            or proofs[0] is not operation or proofs[2]):
        raise HeaderBoundsError("memory_complete_numeric_graph_unavailable")
    # Effect's Source validators: phase entry + perform entry + perform final.
    # Capture's: phase entry + capture entry + Source publication consumer.
    # Completion's: validateRetention + prepareLifecycle + publication consumer.
    # Source snapshots include their own universe/cache path. Engine/reader/
    # first-write common certificates do not execute that universe helper.
    return (
        ("effect", "dispatch-validator", 3),
        ("effect", "dispatch-authority", 2),
        ("effect", "original-memory-owner", 1),
        ("effect", "prepared-apply", 1),
        ("effect", "prepared-outer-validation", 1),
        ("effect", "prepared-consumed-validation", 1),
        ("effect", "prepared-audit", 1),
        ("effect", "first-write-common", 1),
        ("effect", "generic-body", 1),
        ("capture", "dispatch-validator", 3),
        ("capture", "dispatch-authority", 1),
        ("capture", "source-snapshot", 2),
        ("capture", "engine-common", 1),
        ("capture", "generic-body", 1),
        ("capture", "cold-readback", 1),
        ("capture", "all-source-rows", 1),
        ("capture", "row-witnesses", 1),
        ("capture", "engine-publication", 1),
        ("completion", "dispatch-validator", 3),
        ("completion", "source-snapshot", 3),
        ("completion", "reader-common", 1),
        ("completion", "writer-superset", 1),
        ("completion", "first-write-common", 1),
        ("completion", "engine-common", 1),
        ("completion", "generic-body", 3),
        ("completion", "cold-readback", 2),
        ("completion", "own-source-rows", 1),
        ("completion", "all-source-rows", 1),
        ("completion", "row-witnesses", 1),
        ("completion", "engine-publication", 1),
        ("completion", "writer-snapshot-fields", 1),
        ("completion", "claim-verifier", 1),
        ("completion", "goal-fence", 1))


def _native_memory_capture_planning_upper(operation, prepared, *, certificate,
        facts, retention_widths):
    """Storage for ONE capture forecast constructor, before it is called.

    This prices numeric maps/lists and the source-labelled pair vector, not
    future payloads. Original Source candidate/context and binding builders
    have their own closed scalar planning companions.
    """
    from src.runtime_plugins import memory_producer
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width as width
    memory_producer._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard = operation.db.info["composition_guard"]
    projection = guard._native_memory_source_projection
    source_upper_owner = getattr(memory_producer,
        "_original_native_memory_source_schedule_planning_upper", None)
    if source_upper_owner is None:
        raise HeaderBoundsError("memory_capture_source_planning_contribution_unavailable")
    source_upper = source_upper_owner(result_present=False)
    # Same60-entry original binding grammar as the Source planning owner,
    # with this real run identity rather than that owner's None label slot.
    run_width = width(operation.run.run_identity)
    nested = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name = max(nested, len("required-plus-goals-inference-set") + 2,
        4 + len("manifest") + 2 + 20 + len("origin-dependencies") + 2)
    binding_label = 5 + len("composition-binding-parser") + 2 + run_width + name
    binding_pair = 3 + binding_label + 20
    input_pair = 3 + name + 20
    binding_upper = (60 * (binding_label + binding_pair) + 35 * nested
        + 2 + 2 * (2 + 60 * binding_pair + 59)
        + 23 * input_pair + (2 + 23 * input_pair + 22)
        + 2 * (7 * input_pair + (2 + 7 * input_pair + 6))
        + 6 * input_pair + (2 + 6 * input_pair + 5)
        + (2 + 14 * 20 + 13) + (2 + 34 * 20 + 33) + (2 + 6 * 20 + 5))
    cold_label = (2 + 5 + width("native-memory-cold") + width("binding-digest")
        + run_width + width("capture") + 20 + name)
    cold_pair = 3 + cold_label + 20
    payload_names = (len("payload-methods-list") + len("payload-dependencies-list")
        + len("binding-payload") + len("binding-canonical") + len("binding-encoded")
        + len("binding-digest") + len("dependency-payloads"))
    binding_upper += (2 + payload_names + 7 * 2 + 6
        + 21 * (cold_label + cold_pair) + 2 + 2 * (2 + 21 * cold_pair + 20))
    row_count, new_count, update_count = len(facts["row_uppers"]), len(projection[1]), len(projection[2])
    real_count = event_count = event_tuple_width = 0
    max_fields = 0
    field_width = 0
    for descriptor, _key, row in projection[1]:
        max_fields = max(max_fields, len(descriptor.columns))
        if descriptor.table in ("audit_events", "work_board_events"):
            event_count += 1
            event_tuple_width += 3 + width(descriptor.table) + (
                22 if type(_key) is _NativeMemoryFutureIntegerSlot else width(_key))
        for name, kind in zip(descriptor.columns, descriptor.kinds):
            field_width = max(field_width, width(name))
            if kind == "real" and row[name] is not None:
                real_count += 1
    for descriptor, _key, _changes in projection[2]:
        max_fields = max(max_fields, len(descriptor.columns))
        for name in descriptor.columns:
            field_width = max(field_width, width(name))
    from src.memory.header_bounds import WRS_BY_RUN
    max_fields = max(max_fields, len(WRS_BY_RUN.columns))
    for name in WRS_BY_RUN.columns:
        field_width = max(field_width, width(name))
    field_width = max(field_width, width("original_checkpoint_digest"),
        width("original_checkpoint_id"))
    # 100 literal capture appearances; seven/new row, four/update and one
    # non-null real conversion. All literal stage names fit64 ASCII chars.
    # The ordinal and each numeric capacity use the original int64 width20.
    delta_count = 7 * new_count + 4 * update_count + real_count
    ordinary_label = 2 + 2 + width("native-memory-capture") + run_width + 66
    delta_label = ordinary_label + 2 + 3 + 20 + field_width + 66
    # The21 retained binding-digest labels embed the original six-field cold
    # label, including its own run identity and nested manifest stage.
    binding_label = ordinary_label + 2 + 1 + width("binding-digest") + (
        2 + 5 + width("native-memory-cold") + width("binding-digest")
        + run_width + width("capture") + 20 + 2 + 2 + 66 + 20 + 66)
    labels = 100 * ordinary_label + delta_count * delta_label + 21 * binding_label
    pairs = 121 * (3 + 20) + delta_count * (3 + 20) + labels
    count = 121 + delta_count
    # Each add materializes its label/pair and list slot; final tuple copies
    # every pair. Delta staging additionally retains a pair/list before add.
    vector = labels + 3 * pairs + 2 + max(0, count - 1)
    vector += delta_count * (delta_label + 3 + 20) * 2
    # Literal (stage,upper) source rosters and their enclosing tuples.
    vector += 2 * 100 * (3 + 66 + 20)
    # fields_upper creates one numeric tuple per10 fixed calls, one/new row
    # and two/update. Every entry is a numeric width, not the body text.
    vector += (10 + new_count + 2 * update_count) * (2 + max_fields * 21)
    # Reference numeric maps: each event/new/update and the pending own ref.
    vector += (event_count + new_count + update_count + 2) * (2 + 1 + 2 * (7 + 20))
    # Locator4, receipt3, sealed7, pending4 and delta3 numeric field maps.
    vector += (4 + 3 + 7 + 4 + 3) * (field_width + 1 + 20) + 5 * 3
    vector += 3 * (2 + row_count * 21) + 3 * (2 + new_count * 21)
    vector += 3 * (2 + (update_count + 1) * 21) + 2 * (2 + event_count * 21)
    vector += 2 + max(0, event_count - 1) + event_tuple_width
    # Source providers return117 pairs. Capture extends its own list with
    # them and copies them again into its return tuple; their constructors
    # belong to source_upper, these two caller containers do not.
    source_pair = 3 + 20 + 2 + 2 + width("native-memory-source") + 66 + (2 + 2 + 66 + 20 + 66)
    vector += 2 * 117 * (source_pair + 1)
    return vector + source_upper + binding_upper


def _native_memory_capacity_label_width(value, *, operation, prepared, certificate):
    """Numerical storage width for an authentic Source capacity label.

    A private integer slot occupies capacity only. It never becomes a
    reference, certificate key, permission, SQL value or fabricated identity.
    """
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if type(value) is _NativeMemoryFutureIntegerSlot:
        value._checked()
        if (value.operation is not operation or value.prepared is not prepared
                or value.budget is not certificate.budget or value.bound_key is not None):
            raise HeaderBoundsError("memory_future_integer_slot_changed")
        # Covers the original int64 address and its quoted canonical key.
        return 22
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise HeaderBoundsError("header_metadata_bound")
        return 20
    if type(value) is str:
        return _native_memory_ascii_string_width(value)
    if type(value) is tuple:
        upper = 2 + max(0, len(value) - 1)
        for item in value:
            upper += _native_memory_capacity_label_width(item, operation=operation,
                prepared=prepared, certificate=certificate)
        return upper
    raise HeaderBoundsError("memory_numeric_capacity_label_unavailable")


def _native_memory_issue_body_numeric_contributions(operation, prepared, *,
        certificate, facts, retention_widths):
    """Issue approved constructor contributions once on the original writer.

    The retained immutable vectors carry capacity dimensions only. They do
    not certify future bodies or authorize the effect or its publication.
    """
    from src.runtime_plugins.memory_producer import _native_memory_dispatch_validation_numeric_owner
    from src.memory.header_bounds import HeaderBoundsError
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard = operation.db.info["composition_guard"]
    if (getattr(guard, "_native_memory_generic_body_allocation_numeric", None) is not None
            or getattr(guard, "_native_memory_row_witness_allocation_numeric", None) is not None):
        raise HeaderBoundsError("memory_numeric_constructor_already_issued")
    # This scalar includes the complete generic/cold constructor subtree and
    # this exact five-pointer lineage tuple. It excludes common certification.
    body_planning = _native_memory_generic_body_construction_numeric_upper(
        operation, prepared, certificate=certificate, facts=facts,
        retention_widths=retention_widths)
    guard.header_budget.debit(body_planning,
        appearance=("native-memory-composer", "generic-body-constructor"))
    body = _native_memory_generic_future_numeric_schedule(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    guard._native_memory_generic_body_allocation_numeric = (
        operation, prepared, certificate, retention_widths, body)
    # The witness scalar covers its nested four-stage vectors and its output
    # list/tuple. The retained pointer tuple is a separate actual allocation.
    witness_planning = _native_memory_row_witness_collection_numeric_entries(
        operation, prepared, certificate=certificate, facts=facts,
        retention_widths=retention_widths, _construction_only=True)
    guard.header_budget.debit(witness_planning,
        appearance=("native-memory-composer", "row-witness-constructor"))
    witnesses = _native_memory_row_witness_collection_numeric_entries(
        operation, prepared, certificate=certificate, facts=facts,
        retention_widths=retention_widths)
    guard.header_budget.debit(22,
        appearance=("native-memory-composer", "row-witness-lineage"))
    guard._native_memory_row_witness_allocation_numeric = (
        operation, prepared, certificate, retention_widths, witnesses)


def _native_memory_retained_phase_numeric_entries(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """Rephase already issued contributions, preserving every appearance.

    This contribution is not the complete reservation. Standalone cold,
    Current, authority and lifecycle owners remain required contributions.
    Scalar mode prices the new containers without constructing their entries.
    """
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    from src.runtime_plugins.memory_producer import _native_memory_dispatch_validation_numeric_owner
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    if type(_construction_only) is not bool:
        raise HeaderBoundsError("memory_numeric_planning_mode_changed")
    guard = operation.db.info["composition_guard"]
    roster = getattr(guard, "_native_memory_effect_numeric_call_vector", None)
    dispatch = getattr(guard, "_native_memory_dispatch_allocation_numeric", None)
    reads = getattr(guard, "_native_memory_dispatch_read_numeric", None)
    capture = getattr(guard, "_native_memory_capture_allocation_numeric", None)
    rows = getattr(guard, "_native_memory_source_rows_allocation_numeric", None)
    common = getattr(guard, "_native_memory_future_common_components", None)
    snapshot = getattr(guard, "_native_memory_source_snapshot_allocation_numeric", None)
    publication = getattr(guard, "_native_memory_publication_allocation_numeric", None)
    journals = getattr(guard, "_native_memory_private_journal_numeric", None)
    body = getattr(guard, "_native_memory_generic_body_allocation_numeric", None)
    witnesses = getattr(guard, "_native_memory_row_witness_allocation_numeric", None)
    # Check identities individually: no validation tuple/map is allocated
    # before the caller has paid this builder's scalar construction bound.
    if (type(roster) is not tuple or len(roster) != 5
            or roster[0] is not operation or roster[1] is not prepared
            or roster[2] is not certificate or roster[3] is not retention_widths
            or type(dispatch) is not tuple or len(dispatch) != 6
            or dispatch[0] is not operation or dispatch[1] is not prepared
            or dispatch[2] is not certificate or dispatch[3] is not facts
            or dispatch[4] is not retention_widths
            or type(reads) is not tuple or len(reads) != 9
            or reads[0] is not operation or reads[1] is not prepared
            or reads[2] is not certificate or reads[3] is not facts
            or reads[4] is not certificate.budget
            or reads[5] is not guard._native_memory_source_capacity_owner[3]
            or reads[6] is not operation.transaction or reads[7] is not operation.driver
            or type(capture) is not tuple or len(capture) != 5
            or capture[0] is not operation or capture[1] is not prepared
            or capture[2] is not certificate or capture[3] is not retention_widths
            or type(rows) is not tuple or len(rows) != 6
            or rows[0] is not operation or rows[1] is not prepared
            or rows[2] is not certificate or rows[3] is not facts
            or rows[4] is not retention_widths
            or type(common) is not tuple or len(common) != 6
            or common[0] is not operation or common[1] is not prepared
            or common[2] is not certificate or common[3] is not facts
            or common[4] is not retention_widths
            or type(snapshot) is not tuple or len(snapshot) != 6
            or snapshot[0] is not operation or snapshot[1] is not prepared
            or snapshot[2] is not certificate or snapshot[3] is not facts
            or snapshot[4] is not retention_widths
            or type(publication) is not tuple or len(publication) != 6
            or publication[0] is not operation or publication[1] is not prepared
            or publication[2] is not certificate or publication[3] is not retention_widths
            or type(journals) is not tuple or len(journals) != 7
            or journals[0] is not operation or journals[1] is not prepared
            or journals[2] is not certificate or journals[3] is not retention_widths
            or type(body) is not tuple or len(body) != 5
            or body[0] is not operation or body[1] is not prepared
            or body[2] is not certificate or body[3] is not retention_widths
            or type(witnesses) is not tuple or len(witnesses) != 5
            or witnesses[0] is not operation or witnesses[1] is not prepared
            or witnesses[2] is not certificate or witnesses[3] is not retention_widths):
        raise HeaderBoundsError("memory_retained_phase_contribution_unavailable")
    current = getattr(guard, "_native_memory_current_allocation_numeric", None)
    cold = getattr(guard, "_native_memory_standalone_cold_allocation_numeric", None)
    original = getattr(guard, "_native_memory_original_preflight_allocation_numeric", None)
    if (type(current) is not tuple or len(current) != 5
            or current[0] is not operation or current[1] is not prepared
            or current[2] is not certificate or current[3] is not retention_widths
            or type(cold) is not tuple or len(cold) != 5
            or cold[0] is not operation or cold[1] is not prepared
            or cold[2] is not certificate or cold[3] is not retention_widths
            or type(original) is not tuple or len(original) != 5
            or original[0] is not operation or original[1] is not prepared
            or original[2] is not certificate or original[3] is not retention_widths):
        raise HeaderBoundsError("memory_reference_phase_contribution_unavailable")
    completion_claim = getattr(guard, "_native_memory_completion_claim_read_allocation_numeric", None)
    completion_verifier = getattr(guard, "_native_memory_completion_verifier_allocation_numeric", None)
    completion_goal = getattr(guard, "_native_memory_completion_goal_allocation_numeric", None)
    completion_output = getattr(guard, "_native_memory_completion_output_allocation_numeric", None)
    for component in range(4):
        if component == 0:
            lineage = completion_claim
        elif component == 1:
            lineage = completion_verifier
        elif component == 2:
            lineage = completion_goal
        else:
            lineage = completion_output
        if (type(lineage) is not tuple or len(lineage) != 5
                or lineage[0] is not operation or lineage[1] is not prepared
                or lineage[2] is not certificate or lineage[3] is not retention_widths
                or type(lineage[4]) is not tuple):
            raise HeaderBoundsError("memory_completion_phase_contribution_unavailable")
    mutation = getattr(guard, "_native_memory_mutation_allocation_numeric", None)
    if (type(mutation) is not tuple or len(mutation) != 5
            or mutation[0] is not operation or mutation[1] is not prepared
            or mutation[2] is not certificate or mutation[3] is not retention_widths
            or type(mutation[4]) is not tuple):
        raise HeaderBoundsError("memory_mutation_phase_contribution_unavailable")
    prepared_local = getattr(guard, "_native_memory_prepared_local_allocation_numeric", None)
    prepared_outer = getattr(guard, "_native_memory_prepared_outer_allocation_numeric", None)
    prepared_final = getattr(guard, "_native_memory_prepared_final_allocation_numeric", None)
    prepared_slot = getattr(guard, "_native_memory_prepared_slot_allocation_numeric", None)
    prepared_audit = getattr(guard, "_native_memory_prepared_audit_allocation_numeric", None)
    issued_slots = getattr(guard, "_native_memory_integer_slots", None)
    if (type(issued_slots) is not tuple or len(issued_slots) != 2
            or issued_slots[0] is not operation or type(issued_slots[1]) is not tuple):
        raise HeaderBoundsError("memory_prepared_numeric_slot_cohort_changed")
    for component in range(5):
        if component == 0:
            lineage = prepared_local
        elif component == 1:
            lineage = prepared_outer
        elif component == 2:
            lineage = prepared_final
        elif component == 3:
            if not issued_slots[1]:
                if prepared_slot is not None:
                    raise HeaderBoundsError("memory_prepared_numeric_slot_cohort_changed")
                continue
            lineage = prepared_slot
        else:
            lineage = prepared_audit
        if (type(lineage) is not tuple or len(lineage) != 5
                or lineage[0] is not operation or lineage[1] is not prepared
                or lineage[2] is not certificate or lineage[3] is not retention_widths
                or type(lineage[4]) is not tuple):
            raise HeaderBoundsError("memory_prepared_phase_contribution_unavailable")
    sql_capture = getattr(guard, "_native_memory_capture_sql_allocation_numeric", None)
    sql_completion = getattr(guard, "_native_memory_completion_sql_allocation_numeric", None)
    for component in range(2):
        lineage = sql_capture if component == 0 else sql_completion
        if (type(lineage) is not tuple or len(lineage) != 5
                or lineage[0] is not operation or lineage[1] is not prepared
                or lineage[2] is not certificate or lineage[3] is not retention_widths
                or type(lineage[4]) is not tuple):
            raise HeaderBoundsError("memory_sql_phase_contribution_unavailable")
    accounting_certification = getattr(guard, "_native_memory_accounting_certification_allocation_numeric", None)
    if (type(accounting_certification) is not tuple or len(accounting_certification) != 5
            or accounting_certification[0] is not operation
            or accounting_certification[1] is not prepared
            or accounting_certification[2] is not certificate
            or accounting_certification[3] is not retention_widths
            or type(accounting_certification[4]) is not tuple):
        raise HeaderBoundsError("memory_accounting_certification_phase_unavailable")
    dispatch_authority = getattr(guard, "_native_memory_dispatch_authority_allocation_numeric", None)
    completion_authority = getattr(guard, "_native_memory_completion_authority_allocation_numeric", None)
    completion_certificates = getattr(guard, "_native_memory_completion_certificate_allocation_numeric", None)
    for component in range(3):
        if component == 0:
            lineage = dispatch_authority
        elif component == 1:
            lineage = completion_authority
        else:
            lineage = completion_certificates
        if (type(lineage) is not tuple or len(lineage) != 5
                or lineage[0] is not operation or lineage[1] is not prepared
                or lineage[2] is not certificate or lineage[3] is not retention_widths
                or type(lineage[4]) is not tuple):
            raise HeaderBoundsError("memory_authority_phase_contribution_unavailable")
    accounting_payload = getattr(guard, "_native_memory_accounting_payload_allocation_numeric", None)
    if (type(accounting_payload) is not tuple or len(accounting_payload) != 5
            or accounting_payload[0] is not operation or accounting_payload[1] is not prepared
            or accounting_payload[2] is not certificate or accounting_payload[3] is not retention_widths
            or type(accounting_payload[4]) is not tuple):
        raise HeaderBoundsError("memory_accounting_payload_phase_unavailable")
    lookup = getattr(guard, "_native_memory_reference_lookup_allocation_numeric", None)
    lookup_candidate = getattr(guard, "_native_memory_reference_candidate_allocation_numeric", None)
    for component in range(2):
        lineage = lookup if component == 0 else lookup_candidate
        if (type(lineage) is not tuple or len(lineage) != 5
                or lineage[0] is not operation or lineage[1] is not prepared
                or lineage[2] is not certificate or lineage[3] is not retention_widths
                or type(lineage[4]) is not tuple):
            raise HeaderBoundsError("memory_reference_lookup_phase_unavailable")
    count = storage = 0
    if not _construction_only:
        result = []
    # Two passes through each validator source preserve allocations and reads
    # as distinct vectors; common core and MISS wrapper are likewise distinct.
    for phase, owner, occurrences in roster[4]:
        for component in range(2):
            triples = False
            entries = None
            if owner == "dispatch-validator":
                entries = dispatch[5] if component == 0 else reads[8]
                triples = component == 0
            elif owner == "source-snapshot" and component == 0:
                entries = snapshot[5]
            elif owner == "all-source-rows" and component == 0:
                entries = rows[5]["all"]
            elif owner == "own-source-rows" and component == 0:
                entries = rows[5]["own"]
            elif owner == "generic-body" and component == 0:
                entries = body[4]
            elif owner == "row-witnesses" and component == 0:
                entries = witnesses[4]
            elif owner in {"first-write-common", "engine-common", "reader-common", "writer-superset"}:
                entries = common[5][component][1]
            if entries is None:
                continue
            for _occurrence in range(occurrences):
                for entry in entries:
                    if triples:
                        label = entry[1]
                        amount = entry[2]
                    else:
                        label = entry[0]
                        amount = entry[1]
                    if type(amount) is not int or amount < 0:
                        raise HeaderBoundsError("canonical_bound_not_certified")
                    width = 4 + _native_memory_ascii_string_width(phase) + 20
                    width += _native_memory_capacity_label_width(label, operation=operation,
                        prepared=prepared, certificate=certificate)
                    count += 1
                    storage += width + 1
                    if not _construction_only:
                        result.append((phase, label, amount))
    # Capture and the two publication/journal phases are direct original calls.
    for component in range(32):
        if component == 0:
            entries = capture[4]
            phase = "capture"
            triples = False
        elif component == 1:
            entries = publication[5]
            phase = None
            triples = True
        elif component == 2:
            entries = journals[4]
            phase = "capture"
            triples = False
        elif component == 3:
            entries = journals[5]
            phase = "completion"
            triples = False
        elif component == 4:
            entries = current[4]
            phase = None
            triples = True
        elif component in {5, 6, 7}:
            entries = cold[4]
            phase = "capture" if component == 5 else "completion"
            triples = False
        elif component == 8:
            entries = original[4]
            phase = "capture"
            triples = False
        elif component == 9:
            entries = completion_claim[4]
            phase = "completion"
            triples = False
        elif component == 10:
            entries = completion_verifier[4]
            phase = "completion"
            triples = True
        elif component == 11:
            entries = completion_goal[4]
            phase = "completion"
            triples = True
        elif component == 12:
            entries = completion_output[4]
            phase = "completion"
            triples = True
        elif component == 13:
            entries = mutation[4]
            phase = "effect"
            triples = True
        elif component in {19, 20}:
            entries = sql_capture[4] if component == 19 else sql_completion[4]
            phase = "capture" if component == 19 else "completion"
            triples = False
        elif component in {21, 22}:
            entries = accounting_certification[4]
            phase = "capture" if component == 21 else "completion"
            triples = False
        elif component in {23, 24, 25}:
            entries = dispatch_authority[4]
            phase = "effect" if component != 25 else "capture"
            triples = False
        elif component == 26:
            entries = completion_authority[4]
            phase = "completion"
            triples = False
        elif component == 27:
            entries = completion_certificates[4]
            phase = "completion"
            triples = False
        elif component in {28, 29}:
            entries = accounting_payload[4]
            phase = "capture" if component == 28 else "completion"
            triples = False
        elif component in {30, 31}:
            entries = lookup[4] if component == 30 else lookup_candidate[4]
            phase = "capture"
            triples = False
        else:
            if component == 14:
                entries = prepared_local[4]
            elif component == 15:
                entries = prepared_outer[4]
            elif component == 16:
                entries = prepared_final[4]
            elif component == 17:
                if not issued_slots[1]:
                    continue
                entries = prepared_slot[4]
            else:
                entries = prepared_audit[4]
            phase = "effect"
            triples = True
        for entry in entries:
            if triples:
                phase, label, amount = entry
            else:
                label, amount = entry
            if phase not in {"effect", "capture", "completion"} or type(amount) is not int or amount < 0:
                raise HeaderBoundsError("canonical_bound_not_certified")
            width = 4 + _native_memory_ascii_string_width(phase) + 20
            width += _native_memory_capacity_label_width(label, operation=operation,
                prepared=prepared, certificate=certificate)
            count += 1
            storage += width + 1
            if not _construction_only:
                result.append((phase, label, amount))
    if _construction_only:
        # Triple objects, list slots and final tuple slots; iteration controls
        # are bounded by the literal roster and preserve all multiplicities.
        return 4 + 3 * storage + (count + 8 * len(roster[4]) + 102) * 20
    return tuple(result)


def _native_memory_complete_label_planning_width(value, *, operation, prepared, certificate):
    """Scalar final-storage bound, including only genuinely issued slots."""
    from src.memory.header_bounds import HeaderBoundsError
    if type(value) is _NativeMemoryFutureIntegerSlot:
        value._checked()
        if (value.operation is not operation or value.prepared is not prepared
                or value.budget is not certificate.budget or value.bound_key is not None):
            raise HeaderBoundsError("memory_future_integer_slot_changed")
        return 22
    if type(value) is tuple:
        width = 2 + max(0, len(value) - 1)
        for item in value:
            width += _native_memory_complete_label_planning_width(item,
                operation=operation, prepared=prepared, certificate=certificate)
        return width
    return _native_memory_accounting_label_planning_width(value)


async def _native_memory_effect_numeric_schedule(db, run, operation, prepared, *,
        existing_references, before_row, certificate):
    """Compose only the original Source's closed, paid numeric contributions.

    An unresolved owner contribution is a denial before ledger issuance. The
    retained progress tuple describes already closed contributions; it is not
    a partial reservation, certificate, reference roster or read permission.
    """
    from src.memory.header_bounds import HeaderBoundsError
    from src.runtime_plugins import memory_producer
    guard = db.info.get("composition_guard")
    retained = getattr(guard, "_native_memory_source_capacity_facts", None)
    dimensions = getattr(guard, "_native_memory_retention_numeric_facts", None)
    if (type(retained) is not tuple or len(retained) != 4
            or type(dimensions) is not tuple or len(dimensions) != 4
            or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or dimensions[0] is not operation
            or dimensions[1] is not prepared or dimensions[2] is not certificate):
        raise HeaderBoundsError("memory_numeric_forecast_owner_changed")
    facts, widths = retained[3], dimensions[3]
    # Authenticate before constructing any generic row map or numeric vector.
    # Each later projection has its own prepaid constructor contribution.
    memory_producer._native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    selected_before = None
    for descriptor, key, row, _encoded in guard._native_memory_existing_source_rows[2]:
        if descriptor.table == "workflow_run_states" and key == run.run_identity:
            selected_before = row
    if (guard.db is not db or operation.run is not run
            or selected_before != before_row
            or existing_references != guard._native_memory_existing_source_rows[1]):
        raise HeaderBoundsError("memory_numeric_forecast_owner_changed")
    call_vector = _native_memory_minimal_call_vector(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    # The roster is a literal source constant, not a constructed projection.
    # Retain only its original-owner lineage after paying the pointer tuple.
    guard.header_budget.debit(2 + 4 + 5 * 20,
        appearance=("native-memory-composer", "call-vector-lineage"))
    guard._native_memory_effect_numeric_call_vector = (
        operation, prepared, certificate, widths, call_vector)
    publication = getattr(guard, "_native_memory_publication_allocation_numeric", None)
    journals = getattr(guard, "_native_memory_private_journal_numeric", None)
    if (type(publication) is not tuple or len(publication) != 6
            or publication[0] is not operation or publication[1] is not prepared
            or publication[2] is not certificate or publication[3] is not widths
            or type(publication[5]) is not tuple
            or type(journals) is not tuple or len(journals) != 7
            or journals[0] is not operation or journals[1] is not prepared
            or journals[2] is not certificate or journals[3] is not widths):
        raise HeaderBoundsError("memory_numeric_publication_contribution_unavailable")
    capture_upper = completion_upper = appearances = 0
    for phase, _appearance, upper in publication[5]:
        if type(upper) is not int or upper < 0:
            raise HeaderBoundsError("canonical_bound_not_certified")
        if phase == "capture":
            capture_upper += upper
        elif phase == "completion":
            completion_upper += upper
        else:
            raise HeaderBoundsError("memory_numeric_publication_contribution_unavailable")
        appearances += 1
    for _appearance, upper in journals[4]:
        capture_upper += upper
        appearances += 1
    for _appearance, upper in journals[5]:
        completion_upper += upper
        appearances += 1
    for _proof, entries in journals[6]:
        for _appearance, upper in entries:
            capture_upper += upper
            appearances += 1
    # Six fixed scalar/pointer slots are paid before this progress record is
    # materialized. No future appearance is enrolled by this diagnostic record.
    guard.header_budget.debit(2 + 5 + 6 * 20,
        appearance=("native-memory-composer", "closed-contribution-progress"))
    guard._native_memory_effect_numeric_progress = (
        operation, prepared, certificate, appearances, capture_upper, completion_upper)
    dispatch_planning_owner = getattr(memory_producer,
        "_original_native_memory_dispatch_planning_upper", None)
    if dispatch_planning_owner is None:
        raise HeaderBoundsError("memory_dispatch_planning_contribution_unavailable")
    dispatch_planning = dispatch_planning_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(dispatch_planning,
        appearance=("native-memory-composer", "dispatch-constructor"))
    dispatch_allocations = memory_producer.original_native_memory_dispatch_validation_numeric_schedule(
        operation, prepared, certificate=certificate, facts=facts,
        retention_widths=widths, result_present=True)
    if type(dispatch_allocations) is not tuple or len(dispatch_allocations) != 185:
        raise HeaderBoundsError("memory_dispatch_numeric_source_shape_changed")
    from src.memory.composition_headers import _common_trace_label_width
    dispatch_vector_width = 2 + max(0, len(dispatch_allocations) - 1)
    for _phase, label, _amount in dispatch_allocations:
        dispatch_vector_width += 4 + len("effect") + 2 + _common_trace_label_width(label) + 20
    guard.header_budget.debit(2 + 5 + 5 * 20 + dispatch_vector_width,
        appearance=("native-memory-composer", "dispatch-lineage"))
    guard._native_memory_dispatch_allocation_numeric = (
        operation, prepared, certificate, facts, widths, dispatch_allocations)
    dispatch_reads = await memory_producer.original_native_memory_dispatch_validation_read_numeric_schedule(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    # Issue the capture vector exactly once on this original pre-effect
    # writer. The later capture consumes these same objects, not a second
    # helper allocation after M5 has changed the database.
    capture_planning = _native_memory_capture_planning_upper(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(capture_planning,
        appearance=("native-memory-composer", "capture-constructor"))
    capture_allocations = _native_memory_capture_allocation_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=widths)
    capture_vector_width = 2 + max(0, len(capture_allocations) - 1)
    capture_map_upper, capture_map_count = 2, 0
    for label, _amount in capture_allocations:
        capture_vector_width += 3 + _common_trace_label_width(label) + 20
        if (type(label) is tuple and len(label) >= 3
                and label[0] == "native-memory-capture" and label[1] == run.run_identity):
            capture_map_count += 1
            capture_map_upper += _common_trace_label_width(label[2]) + 1 + 20
    capture_map_upper += max(0, capture_map_count - 1)
    guard.header_budget.debit(2 + 4 + 4 * 20 + capture_vector_width,
        appearance=("native-memory-composer", "capture-lineage"))
    guard._native_memory_capture_allocation_numeric = (
        operation, prepared, certificate, widths, capture_allocations)
    guard.header_budget.debit(2 + 5 + 6 * 20,
        appearance=("native-memory-composer", "capture-stage-map-lineage"))
    guard._native_memory_capture_stage_map_numeric = (
        operation, prepared, certificate, widths, capture_map_upper,
        capture_map_count)
    source_rows_planning = _native_memory_source_rows_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths,
        _construction_only=True)
    guard.header_budget.debit(source_rows_planning,
        appearance=("native-memory-composer", "source-rows-constructor"))
    source_rows = _native_memory_source_rows_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    source_rows_width = 2 + 1
    for name in source_rows:
        entries = source_rows[name]
        source_rows_width += _common_trace_label_width(name) + 1 + 2 + max(0, len(entries) - 1)
        for label, _amount in entries:
            source_rows_width += 3 + _common_trace_label_width(label) + 20
    guard.header_budget.debit(2 + 5 + 5 * 20 + source_rows_width,
        appearance=("native-memory-composer", "source-rows-lineage"))
    guard._native_memory_source_rows_allocation_numeric = (
        operation, prepared, certificate, facts, widths, source_rows)
    common_planning = _native_memory_future_common_numeric_components(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths,
        _construction_only=True)
    guard.header_budget.debit(common_planning,
        appearance=("native-memory-composer", "common-constructor"))
    common_components = _native_memory_future_common_numeric_components(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    common_vector_width = 2 + max(0, len(common_components) - 1)
    for name, entries in common_components:
        common_vector_width += 3 + _common_trace_label_width(name) + 2 + max(0, len(entries) - 1)
        for label, _amount in entries:
            common_vector_width += 3 + _native_memory_capacity_label_width(label,
                operation=operation, prepared=prepared, certificate=certificate) + 20
    guard.header_budget.debit(2 + 5 + 5 * 20 + common_vector_width,
        appearance=("native-memory-composer", "common-lineage"))
    guard._native_memory_future_common_components = (
        operation, prepared, certificate, facts, widths, common_components)
    snapshot_planning = _native_memory_source_snapshot_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths,
        _construction_only=True)
    guard.header_budget.debit(snapshot_planning,
        appearance=("native-memory-composer", "source-snapshot-constructor"))
    snapshot_entries = _native_memory_source_snapshot_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    snapshot_vector_width = 2 + max(0, len(snapshot_entries) - 1)
    for label, _amount in snapshot_entries:
        snapshot_vector_width += 3 + _native_memory_capacity_label_width(label,
            operation=operation, prepared=prepared, certificate=certificate) + 20
    guard.header_budget.debit(2 + 5 + 5 * 20 + snapshot_vector_width,
        appearance=("native-memory-composer", "source-snapshot-lineage"))
    guard._native_memory_source_snapshot_allocation_numeric = (
        operation, prepared, certificate, facts, widths, snapshot_entries)
    # Approved constructor bounds are paid before the original builders.
    # Their immutable results are then reused across the literal phase roster.
    _native_memory_issue_body_numeric_contributions(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    # One issued reference vector per original builder. The future phase
    # roster reuses these objects and preserves each actual callback repeat.
    current_planning = _native_memory_six_current_construction_numeric_entries(
        operation, prepared, certificate=certificate, retention_widths=widths,
        _construction_only=True)
    guard.header_budget.debit(current_planning,
        appearance=("native-memory-composer", "current-constructor"))
    current_entries = _native_memory_successful_current_lifecycle_numeric_entries(
        operation, prepared, certificate=certificate, retention_widths=widths)
    guard.header_budget.debit(22,
        appearance=("native-memory-composer", "current-lineage"))
    guard._native_memory_current_allocation_numeric = (
        operation, prepared, certificate, widths, current_entries)
    cold_planning = _native_memory_standalone_cold_construction_numeric_upper(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(cold_planning,
        appearance=("native-memory-composer", "standalone-cold-constructor"))
    cold_entries = _native_memory_postoriginal_cold_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(22,
        appearance=("native-memory-composer", "standalone-cold-lineage"))
    guard._native_memory_standalone_cold_allocation_numeric = (
        operation, prepared, certificate, widths, cold_entries)
    original_planning = _native_memory_original_preflight_construction_numeric_upper(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(original_planning,
        appearance=("native-memory-composer", "original-preflight-constructor"))
    original_entries = _native_memory_original_preflight_numeric_entries(operation, prepared,
        certificate=certificate, retention_widths=widths)
    guard.header_budget.debit(22,
        appearance=("native-memory-composer", "original-preflight-lineage"))
    guard._native_memory_original_preflight_allocation_numeric = (
        operation, prepared, certificate, widths, original_entries)
    # Four reviewed completion owner contributions are issued once. Their
    # constructor mode is scalar-only; later defaults reuse the same genuine
    # Source facts and original retained claim-read record.
    from src.workflows import job_runtime as completion_owner
    for component in range(4):
        if component == 0:
            helper = completion_owner.original_native_memory_completion_claim_read_numeric_schedule
            stage = "completion-claim-read-constructor"
        elif component == 1:
            helper = completion_owner.original_native_memory_claim_verifier_allocation_numeric_schedule
            stage = "completion-claim-verifier-constructor"
        elif component == 2:
            helper = completion_owner.original_native_memory_completion_goal_fence_numeric_schedule
            stage = "completion-goal-constructor"
        else:
            helper = completion_owner.original_native_memory_completion_output_numeric_schedule
            stage = "completion-output-constructor"
        planning = helper(operation, prepared, certificate=certificate, facts=facts,
            retention_widths=widths, _construction_only=True)
        guard.header_budget.debit(planning, appearance=("native-memory-composer", stage))
        entries = helper(operation, prepared, certificate=certificate, facts=facts,
            retention_widths=widths)
        if type(entries) is not tuple:
            raise HeaderBoundsError("memory_completion_numeric_source_shape_changed")
        vector_width = 2 + max(0, len(entries) - 1)
        for entry in entries:
            if component == 0:
                label, amount = entry
                vector_width += 3
            else:
                phase, label, amount = entry
                if phase != "completion":
                    raise HeaderBoundsError("memory_completion_numeric_source_shape_changed")
                vector_width += 4 + _common_trace_label_width(phase)
            if type(amount) is not int or amount < 0:
                raise HeaderBoundsError("canonical_bound_not_certified")
            vector_width += _native_memory_capacity_label_width(label, operation=operation,
                prepared=prepared, certificate=certificate) + 20
        guard.header_budget.debit(22 + vector_width,
            appearance=("native-memory-composer", "completion-owner-lineage", component))
        if component == 0:
            guard._native_memory_completion_claim_read_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        elif component == 1:
            guard._native_memory_completion_verifier_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        elif component == 2:
            guard._native_memory_completion_goal_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        else:
            guard._native_memory_completion_output_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
    mutation_planning = memory_producer._native_memory_mutation_allocation_construction_numeric_upper(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(mutation_planning,
        appearance=("native-memory-composer", "mutation-constructor"))
    mutation_entries = memory_producer.original_native_memory_mutation_allocation_numeric_schedule(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    mutation_vector_width = 2 + max(0, len(mutation_entries) - 1)
    for phase, label, amount in mutation_entries:
        if phase != "effect" or type(amount) is not int or amount < 0:
            raise HeaderBoundsError("memory_mutation_numeric_source_shape_changed")
        mutation_vector_width += 4 + _common_trace_label_width(phase) + 20
        mutation_vector_width += _native_memory_capacity_label_width(label,
            operation=operation, prepared=prepared, certificate=certificate)
    guard.header_budget.debit(22 + mutation_vector_width,
        appearance=("native-memory-composer", "mutation-lineage"))
    guard._native_memory_mutation_allocation_numeric = (
        operation, prepared, certificate, widths, mutation_entries)
    # Original prepared owner calls remain separate: local apply, outer
    # unconsumed validation, final consumed validation, optional issued-slot
    # binder validation, and the actual child audit owner call.
    from src.memory import m5 as prepared_owner
    from src.audit.repository import original_native_memory_prepared_audit_numeric_schedule
    issued_slots = getattr(guard, "_native_memory_integer_slots", None)
    if (type(issued_slots) is not tuple or len(issued_slots) != 2
            or issued_slots[0] is not operation or type(issued_slots[1]) is not tuple):
        raise HeaderBoundsError("memory_prepared_numeric_slot_cohort_changed")
    for component in range(5):
        if component == 0:
            kind, consumed = "local", False
        elif component == 4:
            kind, consumed = "audit", False
        else:
            kind, consumed = "validation", component != 1
            if component == 3 and not issued_slots[1]:
                continue
        planning = prepared_owner.original_native_memory_prepared_construction_numeric_upper(
            operation, prepared, certificate=certificate, facts=facts,
            retention_widths=widths, kind=kind, consumed=consumed)
        guard.header_budget.debit(planning,
            appearance=("native-memory-composer", "prepared-constructor", component))
        if component == 0:
            entries = prepared_owner.original_native_memory_prepared_numeric_schedule(prepared)
        elif component == 4:
            entries = original_native_memory_prepared_audit_numeric_schedule(prepared)
        else:
            entries = prepared_owner.original_native_memory_prepared_validation_numeric_schedule(
                prepared, consumed=consumed)
        vector_width = 2 + max(0, len(entries) - 1)
        for phase, label, amount in entries:
            if phase != "effect" or type(amount) is not int or amount < 0:
                raise HeaderBoundsError("memory_prepared_numeric_source_shape_changed")
            vector_width += 4 + _common_trace_label_width(phase) + 20
            vector_width += _native_memory_capacity_label_width(label,
                operation=operation, prepared=prepared, certificate=certificate)
        guard.header_budget.debit(22 + vector_width,
            appearance=("native-memory-composer", "prepared-lineage", component))
        if component == 0:
            guard._native_memory_prepared_local_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        elif component == 1:
            guard._native_memory_prepared_outer_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        elif component == 2:
            guard._native_memory_prepared_final_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        elif component == 3:
            guard._native_memory_prepared_slot_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        else:
            guard._native_memory_prepared_audit_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
    # Each authority owner constructor is prepaid once. Generic perform
    # invokes its original callback twice, then capture invokes it once;
    # completion's child authority and explicit certificates are separate.
    for component in range(3):
        if component == 2:
            planning = completion_owner.original_native_memory_completion_certificate_construction_numeric_upper(
                operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
        else:
            planning = memory_producer.original_native_memory_authority_construction_numeric_upper(
                operation, prepared, certificate=certificate, facts=facts,
                retention_widths=widths, kind="dispatch" if component == 0 else "completion-child")
        guard.header_budget.debit(planning,
            appearance=("native-memory-composer", "authority-owner-constructor", component))
        if component == 0:
            entries = memory_producer.original_native_memory_dispatch_authority_numeric_schedule(
                operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
        elif component == 1:
            entries = memory_producer.original_native_memory_completion_authority_allocation_numeric_schedule(
                operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
        else:
            entries = await completion_owner.original_native_memory_completion_certificate_numeric_schedule(
                operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
        vector_width = 2 + max(0, len(entries) - 1)
        for label, amount in entries:
            if type(amount) is not int or amount < 0:
                raise HeaderBoundsError("memory_authority_numeric_source_shape_changed")
            vector_width += 3 + 20 + _native_memory_capacity_label_width(label,
                operation=operation, prepared=prepared, certificate=certificate)
        guard.header_budget.debit(22 + vector_width,
            appearance=("native-memory-composer", "authority-owner-lineage", component))
        if component == 0:
            guard._native_memory_dispatch_authority_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        elif component == 1:
            guard._native_memory_completion_authority_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        else:
            guard._native_memory_completion_certificate_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
    # The accounting issuer companion returns the SAME genuine retained
    # certifier sequence. Its one generic-input validator has a separate
    # reviewed scalar constructor bound; both engine publications use it.
    accounting_planning = completion_owner._native_memory_completion_bound_input_construction_upper(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(accounting_planning,
        appearance=("native-memory-composer", "accounting-certification-constructor"))
    accounting_entries = original_native_memory_accounting_certification_numeric_schedule(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    accounting_vector_width = 2 + max(0, len(accounting_entries) - 1)
    for label, amount in accounting_entries:
        if type(amount) is not int or amount < 0:
            raise HeaderBoundsError("memory_accounting_certification_source_shape_changed")
        accounting_vector_width += 3 + 20 + _native_memory_capacity_label_width(label,
            operation=operation, prepared=prepared, certificate=certificate)
    guard.header_budget.debit(22 + accounting_vector_width,
        appearance=("native-memory-composer", "accounting-certification-lineage"))
    guard._native_memory_accounting_certification_allocation_numeric = (
        operation, prepared, certificate, widths, accounting_entries)
    accounting_payload_planning = _native_memory_accounting_payload_numeric_entries(
        operation, prepared, certificate=certificate, facts=facts,
        retention_widths=widths, _construction_only=True)
    guard.header_budget.debit(accounting_payload_planning,
        appearance=("native-memory-composer", "accounting-payload-constructor"))
    accounting_payload_entries = _native_memory_accounting_payload_numeric_entries(
        operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
    accounting_payload_vector_width = 2 + max(0, len(accounting_payload_entries) - 1)
    for label, amount in accounting_payload_entries:
        if type(amount) is not int or amount < 0:
            raise HeaderBoundsError("memory_accounting_payload_source_shape_changed")
        accounting_payload_vector_width += 3 + 20 + _native_memory_capacity_label_width(label,
            operation=operation, prepared=prepared, certificate=certificate)
    guard.header_budget.debit(22 + accounting_payload_vector_width,
        appearance=("native-memory-composer", "accounting-payload-lineage"))
    guard._native_memory_accounting_payload_allocation_numeric = (
        operation, prepared, certificate, widths, accounting_payload_entries)
    # Source SQL publication compiles its genuine statement twice per
    # phase. Its owner vector preserves both prepare and consumer appearances.
    for component in range(2):
        kind = "capture" if component == 0 else "completion"
        vector_upper = _native_memory_source_publication_vector_upper(kind)
        guard.header_budget.debit(vector_upper,
            appearance=("native-memory-composer", "sql-constructor-list", kind))
        guard.header_budget.debit(vector_upper,
            appearance=("native-memory-composer", "sql-constructor-tuple", kind))
        entries = _native_memory_source_publication_numeric_entries(operation, prepared,
            certificate=certificate, retention_widths=widths, kind=kind)
        vector_width = 2 + max(0, len(entries) - 1)
        for label, amount in entries:
            if type(amount) is not int or amount < 0:
                raise HeaderBoundsError("memory_sql_numeric_source_shape_changed")
            vector_width += 3 + 20 + _native_memory_capacity_label_width(label,
                operation=operation, prepared=prepared, certificate=certificate)
        guard.header_budget.debit(22 + vector_width,
            appearance=("native-memory-composer", "sql-lineage", kind))
        if component == 0:
            guard._native_memory_capture_sql_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
        else:
            guard._native_memory_completion_sql_allocation_numeric = (
                operation, prepared, certificate, widths, entries)
    # Capture calls memory_owner_references once. That owner's candidate
    # precedes the separate candidate inside its original locator lookup.
    for component in range(2):
        if component == 0:
            lookup_planning = memory_producer.original_native_memory_reference_lookup_construction_numeric_upper(
                operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
        else:
            lookup_planning = memory_producer.original_native_memory_source_candidate_construction_numeric_upper(
                operation, prepared, certificate=certificate, facts=facts, retention_widths=widths)
        guard.header_budget.debit(lookup_planning,
            appearance=("native-memory-composer", "reference-lookup-constructor", component))
        if component == 0:
            lookup_entries = await memory_producer.original_native_memory_reference_lookup_numeric_schedule(
                operation, prepared, certificate=certificate, facts=facts)
        else:
            lookup_entries = memory_producer.original_native_memory_source_candidate_numeric_entries(
                candidate_upper=widths["source_upper"])
        lookup_vector_width = 2 + max(0, len(lookup_entries) - 1)
        for label, amount in lookup_entries:
            if type(amount) is not int or amount < 0:
                raise HeaderBoundsError("memory_reference_lookup_source_shape_changed")
            lookup_vector_width += 3 + 20 + _native_memory_capacity_label_width(label,
                operation=operation, prepared=prepared, certificate=certificate)
        guard.header_budget.debit(22 + lookup_vector_width,
            appearance=("native-memory-composer", "reference-lookup-lineage", component))
        if component == 0:
            guard._native_memory_reference_lookup_allocation_numeric = (
                operation, prepared, certificate, widths, lookup_entries)
        else:
            guard._native_memory_reference_candidate_allocation_numeric = (
                operation, prepared, certificate, widths, lookup_entries)
    phase_planning = _native_memory_retained_phase_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths,
        _construction_only=True)
    guard.header_budget.debit(phase_planning,
        appearance=("native-memory-composer", "retained-phase-constructor"))
    phase_entries = _native_memory_retained_phase_numeric_entries(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths)
    guard.header_budget.debit(22,
        appearance=("native-memory-composer", "retained-phase-lineage"))
    guard._native_memory_retained_phase_allocation_numeric = (
        operation, prepared, certificate, widths, phase_entries)
    effect_available = capture_available = completion_available = 0
    for phase, _label, amount in phase_entries:
        if phase == "effect":
            effect_available += amount
        elif phase == "capture":
            capture_available += amount
        else:
            completion_available += amount
    # Observed Source spending and closed-contribution totals only. The
    # remaining named owner denials below prohibit treating these as whole-U.
    guard.header_budget.debit(2 + 7 + 3 * 4 + 5 * 20,
        appearance=("native-memory-composer", "available-totals-lineage"))
    from src.memory.header_bounds import MAX_BYTES
    guard._native_memory_available_numeric_totals = (
        operation, prepared, certificate, MAX_BYTES - guard.header_budget.remaining,
        effect_available, capture_available, completion_available, len(phase_entries))
    authority_owner = getattr(memory_producer,
        "original_native_memory_source_authority_numeric_schedule", None)
    if authority_owner is None:
        raise HeaderBoundsError("memory_source_authority_numeric_contribution_unavailable")
    # The original authority owner exposes scalar prerequisite planning
    # before its strong default checks. Its named missing observation denies
    # here, rather than treating callable presence as a completed contribution.
    authority_planning = await authority_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=widths,
        _construction_only=True)
    guard.header_budget.debit(authority_planning,
        appearance=("native-memory-composer", "source-authority-constructor"))
    authority_entries = await authority_owner(operation, prepared, certificate=certificate,
        facts=facts, retention_widths=widths)
    # The trusted original owner returns ONE complete immutable invocation,
    # including its nested REPORT/Vault/read/callback occurrences. Its named
    # unresolved-term errors propagate before this conditional final assembly.
    # Do not interpret callable presence or an empty tuple as a contribution.
    if type(authority_entries) is not tuple or not authority_entries:
        raise HeaderBoundsError("memory_source_authority_numeric_contribution_unavailable")
    authority_issuance = getattr(memory_producer,
        "_native_memory_source_authority_numeric_issuance", None)
    if authority_issuance is None:
        raise HeaderBoundsError("memory_source_authority_numeric_issuance_unavailable")
    authority_issuance(operation, prepared, authority_entries, certificate=certificate,
        facts=facts, retention_widths=widths)
    authority_storage = 2 + max(0, len(authority_entries) - 1)
    complete_storage = 2 + max(0, len(phase_entries) + len(authority_entries) - 1)
    for entry in authority_entries:
        if type(entry) is not tuple or len(entry) != 2:
            raise HeaderBoundsError("memory_source_authority_numeric_source_shape_changed")
        label, amount = entry
        if type(amount) is not int or not 0 <= amount <= MAX_BYTES:
            raise HeaderBoundsError("memory_source_authority_numeric_source_shape_changed")
        # This scalar walker constructs no JSON, label tuple or output vector.
        # The original Source owner uses closed old-row labels; unsupported
        # object labels stop before any final rephase/concatenation allocation.
        label_width = _native_memory_complete_label_planning_width(label,
            operation=operation, prepared=prepared, certificate=certificate)
        triple_width = 4 + len("effect") + 2 + label_width + 20
        authority_storage += triple_width
        complete_storage += triple_width
    for phase, label, amount in phase_entries:
        complete_storage += 4 + 6 * len(phase) + 2 + 20
        complete_storage += _native_memory_complete_label_planning_width(label,
            operation=operation, prepared=prepared, certificate=certificate)
    # ONE effect invocation: new triple objects, list/tuple slots and final
    # combined tuple are all paid before they are constructed. Preserve every
    # appearance, amount and FIFO occurrence from both original owner vectors.
    guard.header_budget.debit(3 * authority_storage + complete_storage + 41,
        appearance=("native-memory-composer", "complete-vector-constructor"))
    authority_effect = tuple(("effect", label, amount) for label, amount in authority_entries)
    complete_entries = phase_entries + authority_effect
    guard.header_budget.debit(2 + 5 + 5 * 20 + complete_storage,
        appearance=("native-memory-composer", "complete-vector-lineage"))
    guard._native_memory_complete_allocation_numeric = (
        operation, prepared, certificate, facts, widths, complete_entries)
    return complete_entries


async def begin_native_memory_effect(db, run, claim_ref, admission, *, source_operation, prepared):
    """Reserve the actual Source's complete plan before its first M5 write."""
    from src.runtime_plugins.memory_producer import (
        _validate_memory_owner_source, prospective_memory_existing_references)
    from src.memory.m5 import validate_native_memory_mutation_plan
    from src.memory.header_bounds import (
        COMPOSITION_DESCRIPTORS, _reserve_native_memory_effect_ledger, _trace_memory_numeric_charges)
    source = await _validate_memory_owner_source(db, run, source_operation)
    if (source.admission is not admission or source_operation._prepared is not prepared
            or claim_ref != source._scope.witness["claim_ref"]
            or _MEMORY_RETENTIONS.get(id(source)) is not None):
        raise ProductionWorkspaceReconciliationError("memory_retention_original_source_changed")
    await validate_native_memory_mutation_plan(prepared, db=db,
        candidate_digest=admission.candidate_digest, native_memory_source=source)
    from src.memory.composition_headers import native_memory_certification_numeric_trace
    with _trace_memory_numeric_charges(admission.header_budget) as common_trace:
        certificate = await _native_memory_source_snapshot(db, admission.header_budget)
        issuer_trace = await (await db.connection()).run_sync(lambda connection:
            native_memory_certification_numeric_trace(certificate, header_budget=admission.header_budget))
    db.info["composition_guard"]._native_memory_source_common_numeric = (
        source_operation, certificate, issuer_trace)
    from src.memory.composition_headers import _metadata_size
    admission.header_budget.debit(_metadata_size(common_trace),
        appearance=("memory-source-capacity", "snapshot-trace-copy"))
    db.info["composition_guard"]._native_memory_source_snapshot_numeric = (
        source_operation, certificate, tuple(common_trace))
    rows = await _native_memory_source_rows(db, certificate, (("workflow_run_states", run.run_identity),))
    before = rows[0][2]
    if preflight_native_memory_reference_journal(before["checkpoint_receipts_json"]) != (None, None):
        raise ProductionWorkspaceReconciliationError("memory_retention_original_already_exists")
    existing = await prospective_memory_existing_references(db, run, source_operation)
    references = tuple(sorted({("workflow_run_states", run.run_identity),
        *((item.table, item.row_ref) for item in existing)}))
    selected_rows = await _native_memory_source_rows(db, certificate, references)
    db.info["composition_guard"]._native_memory_existing_source_rows = (
        source_operation, references, selected_rows)
    new_rows = []
    future_slots = []
    for instance in prepared.original_rows():
        descriptor = COMPOSITION_DESCRIPTORS[instance.__tablename__]
        slot = None
        if descriptor.table == "work_board_events" and instance.event_id is None:
            import asyncio
            slot = _NativeMemoryFutureIntegerSlot(source_operation, prepared, instance, descriptor,
                db, source_operation.transaction, source_operation.driver, asyncio.current_task(),
                admission.header_budget, len(future_slots))
            row = _native_memory_planned_sql_row(instance, transient=True, _capacity_slot=slot)
            from types import MappingProxyType
            slot.expected_columns = MappingProxyType({name: value for name, value in row.items()
                if name != descriptor.key})
            slot.prior_keys = frozenset(_composition_address(table, key) for table, key in certificate.rows
                if table == descriptor.table)
            future_slots.append(slot)
        else:
            row = _native_memory_transient_sql_row(instance)
        key = row[descriptor.key]
        if descriptor.table == "work_board_events" and type(key) is int:
            key = _composition_address(descriptor.table, key)
        # An unresolved database-generated address is not a reference grant.
        if slot is not None:
            key = slot
        elif type(key) is not str:
            raise ProductionWorkspaceReconciliationError("memory_retention_future_address_unavailable")
        new_rows.append((descriptor, key, row))
    updates = []
    for instance, key, changes in prepared.original_update_rows():
        descriptor = COMPOSITION_DESCRIPTORS[instance.__tablename__]
        key = _composition_address(descriptor.table, key)
        row = _native_memory_planned_sql_row(instance, changes)
        updates.append((descriptor, key, {name: row[name] for name in changes}))
    affected = await _native_memory_retained_current_roster(db, run, source_operation, certificate)
    db.info["composition_guard"]._native_memory_integer_slots = (source_operation, tuple(future_slots))
    db.info["composition_guard"]._native_memory_source_projection = (
        source_operation, tuple(new_rows), tuple(updates))
    numeric_facts = await _native_memory_source_capacity_facts(db, run, source_operation, prepared, certificate)
    db.info["composition_guard"]._native_memory_retention_numeric_facts = (
        source_operation, prepared, certificate,
        _native_memory_source_retention_widths(source_operation, prepared, before, certificate, numeric_facts))
    _native_memory_source_affected_widths(source_operation, prepared, certificate, affected)
    # Derive private publication dimensions from the original paid writer
    # before issuing a complete reservation. In particular, retain the real
    # old checkpoint/receipt reads; a smaller future output cannot price them.
    guard = db.info["composition_guard"]
    retained_widths = guard._native_memory_retention_numeric_facts[3]
    # Close the supported private-journal branch on the original already-paid
    # Source row BEFORE reserving or arming the first M5 effect. The later CAS
    # rechecks remain separate original paid appearances in these vectors.
    capture_journal = _native_memory_private_journal_numeric_entries(source_operation,
        prepared, certificate=certificate, retention_widths=retained_widths, kind="capture")
    completion_journal = _native_memory_private_journal_numeric_entries(source_operation,
        prepared, certificate=certificate, retention_widths=retained_widths, kind="completion")
    affected_widths = guard._native_memory_affected_numeric_facts[4]
    guard.header_budget.debit(2,
        appearance=("memory-private-journal-planning", "affected-vector-list"))
    affected_journals = []
    for proof in affected:
        numeric = _native_memory_private_journal_numeric_entries(source_operation, prepared,
            certificate=certificate, retention_widths=affected_widths[proof.before["run_identity"]],
            kind="affected-unknown", affected_proof=proof)
        guard.header_budget.debit(3 + 8 + _metadata_size(numeric),
            appearance=("memory-private-journal-planning", "affected-vector-slot"))
        affected_journals.append((proof, numeric))
    guard.header_budget.debit(2 + max(0, len(affected_journals) - 1)
        + sum(3 + 4 + _metadata_size(entries) for _proof, entries in affected_journals),
        appearance=("memory-private-journal-planning", "affected-vector-tuple"))
    guard.header_budget.debit(2 + 5 + 4 * 4 + _metadata_size(capture_journal)
        + _metadata_size(completion_journal)
        + 2 + sum(3 + 4 + _metadata_size(entries) for _proof, entries in affected_journals)
        + max(0, len(affected_journals) - 1),
        appearance=("memory-private-journal-planning", "owner-lineage"))
    guard._native_memory_private_journal_numeric = (source_operation, prepared,
        certificate, retained_widths, capture_journal, completion_journal, tuple(affected_journals))
    await guard._native_memory_publication_numeric_widths(source_operation, prepared,
        certificate=certificate, facts=numeric_facts, retention_widths=retained_widths)
    ledger = await _reserve_native_memory_effect_ledger(db, run, source_operation, prepared,
        existing_references=references, before_row=before, certificate=certificate)
    retention = _NativeMemoryRetention(source, source_operation, prepared, ledger, before,
        tuple(new_rows), tuple(updates), references, affected, tuple(future_slots))
    retention._publication_numeric_facts = db.info["composition_guard"]._native_memory_retention_numeric_facts
    # Numeric lineage survives the original reader's lifetime. It is never a
    # certificate, retained guard or permission to reopen another owner's body.
    retention._affected_publication_numeric_facts = db.info["composition_guard"]._native_memory_affected_numeric_facts
    retention._engine_publication_numeric_facts = getattr(db.info["composition_guard"],
        "_native_memory_publication_numeric_facts", None)
    retention._prior_publication_numeric = getattr(db.info["composition_guard"],
        "_native_memory_prior_publication_numeric", None)
    retention._accounting_numeric = getattr(db.info["composition_guard"],
        "_native_memory_accounting_numeric", None)
    retention._accounting_certification_numeric = getattr(db.info["composition_guard"],
        "_native_memory_accounting_certification_numeric", None)
    object.__setattr__(source, "_retention", retention)
    _MEMORY_RETENTIONS[id(source)] = retention
    db.info["composition_guard"]._native_memory_source_retention = retention
    return retention


def _append_native_memory_original(raw, original):
    """Add Original without rewriting any retained journal lexeme."""
    from src.memory.header_bounds import strict_json_loads
    from src.workflows.job_runtime import _canonical
    records = strict_json_loads(raw)
    if type(records) is not list or any(type(item) is not dict for item in records):
        raise ProductionWorkspaceReconciliationError("memory_reference_journal_invalid")
    if any(item.get("checkpoint_id") in {MEMORY_ORIGINAL_CHECKPOINT, MEMORY_CURRENT_CHECKPOINT}
            for item in records):
        raise ProductionWorkspaceReconciliationError("memory_retention_original_already_exists")
    end = len(raw.rstrip()) - 1
    if end < 0 or raw[end] != "]":
        raise ProductionWorkspaceReconciliationError("memory_reference_journal_invalid")
    return raw[:end] + ("," if records else "") + _canonical(original) + raw[end:]


async def _prepare_native_memory_source_publication(db, run, retention, before, after, certificate, kind, *, pending_changes):
    import asyncio
    import sys
    from sqlalchemy import update
    from src.db.models import WorkflowRunState
    from src.memory.header_bounds import _connection_state
    retention._checked()
    issuer = sys._getframe(1).f_code
    if ((kind == "capture" and issuer is not capture_native_memory_retention.__code__)
            or (kind == "completion" and issuer is not prepare_native_memory_lifecycle.__code__)
            or (kind == "affected-unknown" and issuer is not _native_memory_invalidate_affected_current.__code__)
            or kind not in {"capture", "completion", "affected-unknown"}):
        raise ProductionWorkspaceReconciliationError("memory_publication_original_owner_required")
    facts = retention._publication_numeric_facts
    widths = facts[3]
    if kind == "affected-unknown":
        affected = getattr(retention, "_affected_publication_numeric_facts", None)
        if type(affected) is not tuple or len(affected) != 5:
            raise ProductionWorkspaceReconciliationError("memory_publication_numeric_shape_unavailable")
        widths = affected[4].get(before['run_identity'])
    for copy in ('list', 'tuple'):
        retention.source.admission.header_budget.debit(_native_memory_source_publication_vector_upper(kind),
            appearance=('memory-source-sql', kind, 'numeric-plan-' + copy))
    numeric = _native_memory_source_publication_numeric_entries(retention.operation, retention.prepared,
        certificate=facts[2], retention_widths=widths, kind=kind)
    for appearance, upper in numeric:
        if appearance[-1] in {"pending-copy", "planned-row", "conditions", "statement"}:
            retention.source.admission.header_budget.debit(upper, appearance=appearance)
    pending = dict(pending_changes, checkpoint_receipts_json=after["checkpoint_receipts_json"])
    if kind != "affected-unknown" and _native_memory_planned_sql_row(run, pending) != after:
        raise ProductionWorkspaceReconciliationError("memory_publication_bind_changed")
    conditions = (
        WorkflowRunState.run_identity == before["run_identity"],
        WorkflowRunState.status == before["status"], WorkflowRunState.revision == before["revision"],
        WorkflowRunState.attempt_count == before["attempt_count"],
        WorkflowRunState.lease_owner == before["lease_owner"],
        WorkflowRunState.fencing_token == before["fencing_token"],
        WorkflowRunState.checkpoint_receipts_json == before["checkpoint_receipts_json"],
        WorkflowRunState.checkpoint_context_json == before["checkpoint_context_json"],
    )
    if kind != "affected-unknown":
        conditions += (WorkflowRunState.deadline_at > datetime.now(timezone.utc),
            WorkflowRunState.lease_expires_at > datetime.now(timezone.utc))
    statement = update(WorkflowRunState).where(*conditions).execution_options(
        synchronize_session=False).values(**pending)
    transaction, driver, changes = await _connection_state(db)
    plan = _NativeMemorySourcePublication(retention, db, run, statement, before, after, certificate,
        transaction, driver, changes, asyncio.current_task(), _native_memory_statement_signature(statement, native_memory_retention=retention, kind=kind, publication_widths=widths), kind)
    plan._numeric_widths = widths
    _MEMORY_PUBLICATIONS[id(plan)] = plan
    return plan


def _consume_native_memory_source_publication(db, statement, run_id, before, after, previous, current, plan):
    import asyncio
    from src.memory.composition_headers import current_budget, _validate
    retention = plan.retention._checked()
    if (plan.seal is not _MEMORY_PUBLICATION_SEAL or _MEMORY_PUBLICATIONS.get(id(plan)) is not plan
            or plan.consumed or plan.applied or plan.db is not db or plan.statement is not statement
            or plan.task is not asyncio.current_task() or run_id != plan.before["run_identity"]
            or before != plan.before["checkpoint_receipts_json"] or after != plan.after["checkpoint_receipts_json"]
            or db.sync_session.get_transaction() is not plan.transaction
            or current_budget() is not retention.source.admission.header_budget
            or _native_memory_statement_signature(statement, native_memory_retention=retention, kind=plan.kind, publication_widths=plan._numeric_widths) != plan.signature):
        raise ProductionWorkspaceReconciliationError("memory_publication_source_changed")
    connection = db.sync_session.connection()
    _validate(connection, plan.certificate)
    if (connection.connection.driver_connection is not plan.driver
            or _sql(connection, "SELECT total_changes()").scalar_one() != plan.total_changes):
        raise ProductionWorkspaceReconciliationError("memory_publication_writer_changed")
    _, actual, _ = _native_memory_reference_row_on_connection(connection, "workflow_run_states", run_id)
    if actual != plan.before or current.get(MEMORY_CURRENT_CHECKPOINT) is None:
        raise ProductionWorkspaceReconciliationError("memory_publication_current_changed")
    if plan.kind == "capture":
        if (retention.captured or not retention.prepared.consumed
                or retention.operation._effect is None
                or previous.get(MEMORY_ORIGINAL_CHECKPOINT) is not None
                or previous.get(MEMORY_CURRENT_CHECKPOINT) is not None
                or current.get(MEMORY_ORIGINAL_CHECKPOINT) != retention.original):
            raise ProductionWorkspaceReconciliationError("memory_publication_original_changed")
        permission = (retention.original, current[MEMORY_CURRENT_CHECKPOINT])
    elif plan.kind == "completion":
        if (not retention.captured or retention.completed
                or previous.get(MEMORY_ORIGINAL_CHECKPOINT) != retention.original
                or current.get(MEMORY_ORIGINAL_CHECKPOINT) != retention.original):
            raise ProductionWorkspaceReconciliationError("memory_publication_original_changed")
        permission = (current[MEMORY_CURRENT_CHECKPOINT],)
    elif plan.kind == "affected-unknown":
        proof = plan.affected_proof
        footprint = {(descriptor.table, key) for descriptor, key, _changes in retention.updates}
        replacement = current.get(MEMORY_CURRENT_CHECKPOINT)
        if (type(proof) is not _NativeMemoryRetainedCurrentProof
                or not any(value is proof for value in retention.affected)
                or proof.operation is not retention.operation or proof.task is not plan.task
                or proof.seal is not _MEMORY_PUBLICATION_SEAL
                or proof.connection is not connection
                or proof.guard is not db.info.get("composition_guard")
                or proof.before != plan.before
                or retention.operation._effect is None or not retention.prepared.consumed
                or not any((ref["table"], ref["key"]) in footprint
                    for ref in proof.current["payload"]["refs"])
                or previous.get(MEMORY_ORIGINAL_CHECKPOINT) != proof.original
                or current.get(MEMORY_ORIGINAL_CHECKPOINT) != proof.original
                or previous.get(MEMORY_CURRENT_CHECKPOINT) != proof.current
                or replacement is None or replacement["payload"]["state"] != "unknown"
                or replacement["payload"]["reason_code"] != "current_source_unavailable"
                or replacement["payload"]["refs"] != proof.current["payload"]["refs"]
                or replacement["payload"]["projection_revision"] != proof.current["payload"]["projection_revision"] + 1
                or plan.after["revision"] != plan.before["revision"] + 1
                or any(plan.after[name] != value for name, value in plan.before.items()
                    if name not in {"checkpoint_receipts_json", "revision", "updated_at"})):
            raise ProductionWorkspaceReconciliationError("memory_affected_current_owner_changed")
        permission = (replacement,)
    else:
        raise ProductionWorkspaceReconciliationError("memory_publication_kind_invalid")
    expected = preflight_native_memory_reference_journal(plan.after["checkpoint_receipts_json"])
    if (expected != (current.get(MEMORY_ORIGINAL_CHECKPOINT), current.get(MEMORY_CURRENT_CHECKPOINT))
            or expected[1]["payload"]["state"] != (
                "unknown" if plan.kind == "affected-unknown" else "validated")):
        raise ProductionWorkspaceReconciliationError("memory_publication_current_changed")
    plan.consumed = True
    return permission


def _consume_native_memory_publication(db, statement, run_id, before, after, previous, current):
    """The journal hook can consume only the armed, exact original execute."""
    from src.memory.composition_headers import current_budget, _validate, _certify_current_memory_snapshot_on_connection
    from src.workflows.job_runtime import _MEMORY_NEGATIVE_OWNERS, _original_memory_maintenance_scope
    plan = _ARMED_MEMORY_PUBLICATION.get()
    if type(plan) is _NativeMemorySourcePublication:
        return _consume_native_memory_source_publication(
            db, statement, run_id, before, after, previous, current, plan)
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


def _native_memory_row_witness_numeric_entries(table, key, *, encoded_upper,
        reference_upper, _construction_only=False):
    from src.memory.header_bounds import _native_memory_ascii_string_width as width
    # Exact original three-field object grammar. Numerical sizing requires
    # neither a temporary field dict nor an additional serialized witness.
    witness_upper = 4 + 6 + reference_upper + 15 + 66 + 16 + 7
    if _construction_only:
        label = 5 + width("memory-row-witness") + width(table) + (
            22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)) + width("hash-input")
        pair = 3 + label + 20
        return 4 * label + 4 * pair + 2 + 4 * pair + 3
    return (
        (("memory-row-witness", table, key, "hash-input"), encoded_upper),
        (("memory-row-witness", table, key, "digest"), 66),
        (("memory-row-witness", table, key, "reference"), reference_upper),
        (("memory-row-witness", table, key, "record"), witness_upper))


def _native_memory_row_witness_collection_numeric_entries(operation, prepared, *,
        certificate, facts, retention_widths, _construction_only=False):
    """ONE all-selected-row witness collection, including its own construction.

    Address keys come from the original retained numerical dimensions. An
    unbound integer slot contributes numerical width only; it is not a ref.
    """
    from types import MappingProxyType
    from src.memory.header_bounds import HeaderBoundsError, MAX_ROWS, _native_memory_ascii_string_width as width
    from src.runtime_plugins.memory_producer import _native_memory_dispatch_validation_numeric_owner
    _native_memory_dispatch_validation_numeric_owner(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    if (type(_construction_only) is not bool or type(facts.get("row_uppers")) is not MappingProxyType
            or type(facts.get("raw_tuple_uppers")) is not MappingProxyType
            or not 1 <= len(facts["row_uppers"]) <= MAX_ROWS):
        raise HeaderBoundsError("memory_row_witness_numeric_shape_unavailable")
    rows_count = len(facts["row_uppers"])
    list_upper = 2 + max(0, rows_count - 1)
    constructor_upper = 0
    result_pairs_upper = 0
    for address in facts["row_uppers"]:
        table, key = address
        key_upper = 22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)
        reference_upper = 17 + width(table) + key_upper
        list_upper += 114 + reference_upper
        if _construction_only:
            constructor_upper += _native_memory_row_witness_numeric_entries(table, key,
                encoded_upper=0, reference_upper=reference_upper, _construction_only=True)
            label = 5 + width("memory-row-witness") + width(table) + key_upper + width("hash-input")
            result_pairs_upper += 4 * (3 + label + 20)
    if _construction_only:
        collection_label = 3 + width("memory-row-witness-list") + width("construction")
        collection_pair = 3 + collection_label + 20
        result_pairs_upper += 2 * collection_pair
        # The caller's result list and immutable return copy the same pairs.
        # Two additional collection labels/pairs and two map iterators are
        # distinct from each row helper's constructor and returned tuple.
        return (constructor_upper + 2 * collection_label + 2 * collection_pair
            + 2 * (2 + result_pairs_upper + 4 * rows_count + 1) + 2 * 4)
    result = [(("memory-row-witness-list", "construction"), list_upper)]
    for address in facts["row_uppers"]:
        table, key = address
        reference_upper = 17 + width(table) + (
            22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key))
        own = table == "workflow_run_states" and key == operation.run.run_identity
        encoded_upper = retention_widths["own_raw_upper"] if own else facts["raw_tuple_uppers"][address]
        result.extend(_native_memory_row_witness_numeric_entries(table, key,
            encoded_upper=encoded_upper, reference_upper=reference_upper))
    result.append((("memory-row-witness-list", "return"), list_upper))
    return tuple(result)


def _native_memory_row_witnesses(rows, *, header_budget=None):
    from src.memory.composition_headers import current_budget
    from src.memory.header_bounds import _native_memory_ascii_string_width
    payer = header_budget if header_budget is not None else current_budget()
    if payer is not None:
        list_upper = 2 + max(0, len(rows) - 1)
        for descriptor, key, row, encoded in rows:
            reference_upper = 17 + _native_memory_ascii_string_width(descriptor.table) + _native_memory_ascii_string_width(key)
            list_upper += 4 + 6 + reference_upper + 15 + 66 + 16 + 7
        payer.debit(list_upper, appearance=("memory-row-witness-list", "construction"))
    result = []
    for descriptor, key, row, encoded in rows:
        if payer is not None:
            reference_upper = 17 + _native_memory_ascii_string_width(descriptor.table) + _native_memory_ascii_string_width(key)
            witness_upper = 4 + 6 + reference_upper + 15 + 66 + 16 + 7
            # Debit the four original appearances directly. Constructing a
            # forecast vector here would allocate an unused intermediate after
            # effects; the real witness below is the only output being built.
            payer.debit(len(encoded), appearance=("memory-row-witness", descriptor.table, key, "hash-input"))
            payer.debit(66, appearance=("memory-row-witness", descriptor.table, key, "digest"))
            payer.debit(reference_upper, appearance=("memory-row-witness", descriptor.table, key, "reference"))
            payer.debit(witness_upper, appearance=("memory-row-witness", descriptor.table, key, "record"))
        result.append({"ref": {"table": descriptor.table, "key": key},
            "tuple_digest": hashlib.sha256(encoded).hexdigest(), "encoded_bytes": len(encoded)})
    if payer is not None:
        payer.debit(_native_memory_closed_value_width(result), appearance=("memory-row-witness-list", "return"))
    return result


async def _apply_native_memory_source_publication(db, run, plan):
    from src.runtime_plugins.memory_producer import (
        _validate_memory_owner_source, _validate_memory_lifecycle_source)
    from src.memory.composition_headers import snapshot_reads, snapshot_reads_async
    retention = plan.retention._checked()
    if (type(plan) is not _NativeMemorySourcePublication
            or _MEMORY_PUBLICATIONS.get(id(plan)) is not plan or plan.db is not db
            or plan.run is not run or plan.consumed or plan.applied):
        raise ProductionWorkspaceReconciliationError("memory_publication_plan_unavailable")
    guard = db.info.get("composition_guard")
    if type(guard) is not CompositionSessionGuard or guard.db is not db:
        raise ProductionWorkspaceReconciliationError("memory_publication_guard_missing")
    if plan.kind in {"capture", "affected-unknown"}:
        await _validate_memory_owner_source(db, retention.operation.run, retention.operation, effect=retention.operation._effect)
    else:
        await _validate_memory_lifecycle_source(db, run, retention.source.claim)
    token = _ARMED_MEMORY_PUBLICATION.set(plan)
    try:
        async with snapshot_reads_async(db, plan.certificate):
            result = await db.execute(plan.statement)
        if result.rowcount != 1 or not plan.consumed:
            raise ProductionWorkspaceReconciliationError("memory_publication_cas_changed")
        certificate = await _native_memory_source_snapshot(db, retention.source.admission.header_budget)
        def read_back(connection):
            with snapshot_reads(certificate):
                _, actual, _ = _native_memory_reference_row_on_connection(
                    connection, "workflow_run_states", plan.before["run_identity"])
                if actual != plan.after:
                    raise ProductionWorkspaceReconciliationError("memory_publication_readback_changed")
                _checked_memory_reference_row(connection, actual)
        await (await db.connection()).run_sync(read_back)
        # Populate this exact original ORM instance from actual SQL readback;
        # this does not leave a dirty object to flush a second publication.
        if plan.kind != "affected-unknown":
            retention.source.admission.header_budget.debit(
                certificate.rows[("workflow_run_states", run.run_identity)][1],
                appearance=("memory-source-publication", "refresh", run.run_identity))
            await db.refresh(run)
        plan.applied = True
        return result
    finally:
        _ARMED_MEMORY_PUBLICATION.reset(token)
        # The original completion writer's engine exit consumes this exact
        # applied proof for numerical payment only. All failure/capture paths
        # release it here; guard.publish releases completion on every exit.
        if not (plan.kind == "completion" and plan.consumed and plan.applied):
            _MEMORY_PUBLICATIONS.pop(id(plan), None)


async def _native_memory_invalidate_affected_current(db, retention):
    """Retain uncertainty caused by this exact, already consumed M5 effect.

    The pre-effect accounting owner supplied these concrete validated proofs.
    This never borrows their historical Sources, leases or mutation grants.
    """
    import asyncio
    from sqlalchemy.dialects.sqlite import dialect
    from src.db.models import WorkflowRunState
    from src.memory.header_bounds import WRS_BY_RUN, _native_memory_current_payload_upper
    from src.runtime_plugins.memory_producer import _validate_memory_owner_source
    from src.workflows.job_runtime import _digest
    retention._checked()
    if not retention.affected:
        return
    operation = retention.operation
    await _validate_memory_owner_source(db, operation.run, operation, effect=operation._effect)
    if operation._effect is None or not retention.prepared.consumed:
        raise ProductionWorkspaceReconciliationError("memory_affected_current_owner_changed")
    footprint = {(descriptor.table, key) for descriptor, key, _changes in retention.updates}
    frame = retention.source.admission.header_budget
    for proof in retention.affected:
        if (type(proof) is not _NativeMemoryRetainedCurrentProof
                or proof.operation is not operation or proof.guard is not db.info.get("composition_guard")
                or proof.task is not asyncio.current_task() or proof.seal is not _MEMORY_PUBLICATION_SEAL
                or not any((ref["table"], ref["key"]) in footprint for ref in proof.current["payload"]["refs"])):
            raise ProductionWorkspaceReconciliationError("memory_affected_current_owner_changed")
        certificate = await _native_memory_source_snapshot(db, frame)
        target = proof.before["run_identity"]
        delivered = await _native_memory_source_rows(db, certificate, (("workflow_run_states", target),))
        before = delivered[0][2]
        if before != proof.before:
            raise ProductionWorkspaceReconciliationError("memory_affected_current_before_changed")
        frame.debit(_native_memory_closed_value_width(proof.current["payload"]["refs"]),
            appearance=("memory-affected-unknown", target, "reference-tuples"))
        refs = tuple((ref["table"], ref["key"]) for ref in proof.current["payload"]["refs"])
        payload_upper, record_upper = _native_memory_current_payload_upper(refs, (),
            method=operation._effect.method, unknown_reason="current_source_unavailable")
        def pay(stage, upper):
            frame.debit(upper, appearance=("memory-affected-unknown", target, stage))
        pay("pending-map", certificate.rows[("workflow_run_states", target)][1])
        now = datetime.now(timezone.utc)
        processor = WorkflowRunState.__table__.columns.updated_at.type.dialect_impl(dialect()).bind_processor(dialect())
        updated_at = processor(now) if processor is not None else now
        pending_raw = {"revision": before["revision"] + 1, "updated_at": updated_at}
        if not -(2**63) <= pending_raw["revision"] < 2**63:
            raise ProductionWorkspaceReconciliationError("memory_affected_current_revision_unavailable")
        pay("delta-construction", certificate.rows[("workflow_run_states", target)][1])
        delta = _native_memory_selected_delta_preimage([], [(WRS_BY_RUN, target, pending_raw)], [])
        delta_upper = _native_memory_closed_value_width(delta)
        for stage in ("delta-canonical", "delta-utf8"):
            pay(stage, delta_upper)
        pay("delta-digest", 66)
        for stage in ("original-canonical", "original-utf8"):
            pay(stage, _native_memory_closed_value_width(proof.original))
        pay("original-digest", 66)
        pay("payload-construction", payload_upper)
        payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
            "original_checkpoint_digest": _digest(proof.original),
            "projection_revision": proof.current["payload"]["projection_revision"] + 1,
            "state": "unknown", "reason_code": "current_source_unavailable",
            "refs": proof.current["payload"]["refs"], "absences": [], "rows": [],
            "rows_digest": None, "encoded_bytes": 0,
            "owner_operation_kind": operation._effect.method, "owner_events": [],
            "selected_delta_digest": _digest(delta)}
        for _ in range(32):
            size = preflight_native_memory_reference_payload(payload, current=True, header_budget=frame)
            pay("encoded-count", size)
            if payload["encoded_bytes"] == size:
                break
            payload["encoded_bytes"] = size
        else:
            raise ProductionWorkspaceReconciliationError("memory_unknown_count_not_convergent")
        for stage in ("payload-canonical", "payload-utf8"):
            pay(stage, payload_upper)
        pay("payload-digest", 66)
        pay("record-construction", record_upper)
        replacement = {"checkpoint_id": MEMORY_CURRENT_CHECKPOINT, "safe": True,
            "state_digest": _digest(payload), "payload": payload}
        journal = _splice_native_memory_current_checkpoint(before["checkpoint_receipts_json"],
            replacement, header_budget=frame)
        pay("row-copy", certificate.rows[("workflow_run_states", target)][1])
        after = dict(before, **pending_raw, checkpoint_receipts_json=journal)
        # These are the original SQL typed binds; the raw projection above is
        # checked by the private publication hook and real SQL readback.
        plan = await _prepare_native_memory_source_publication(db, operation.run, retention,
            before, after, certificate, "affected-unknown", pending_changes={
                "revision": pending_raw["revision"], "updated_at": now})
        plan.affected_proof = proof
        await _apply_native_memory_source_publication(db, operation.run, plan)


def _native_memory_capture_allocation_numeric_entries(operation, prepared, *, certificate, retention_widths):
    """ONE capture allocation vector, not its authority/read/publication vector.

    The retained pre-effect certificate supplies numeric provenance only.
    Actual capture still obtains and validates its original current readset.
    """
    import asyncio
    from types import MappingProxyType
    from src.memory.header_bounds import (HeaderBoundsError, MAX_BYTES, MAX_ROWS,
        _native_memory_ascii_string_width as width, _native_memory_object_width as obj,
        _native_memory_array_width as array, WRS_BY_RUN)
    from src.memory.composition_headers import CompositionHeaderCertificate, _SEAL
    from src.memory.m5 import PreparedNativeMemoryMutation, _NATIVE_MUTATION_PLANS
    from src.runtime_plugins import memory_producer as source_owner
    guard = operation.db.info.get("composition_guard") if type(operation) is source_owner._MemoryOwnerSourceOperation else None
    retained = getattr(guard, "_native_memory_retention_numeric_facts", None)
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    capacity = getattr(guard, "_native_memory_source_capacity_facts", None)
    projection = getattr(guard, "_native_memory_source_projection", None)
    if (type(guard) is not CompositionSessionGuard or guard.db is not operation.db
            or source_owner._MEMORY_OPERATIONS.get(id(operation)) is not operation
            or operation._seal is not source_owner._MEMORY_SOURCE_SEAL or operation._issued_id != id(operation)
            or source_owner._MEMORY_SOURCES.get(id(operation.source)) is not operation.source
            or operation.source._seal is not source_owner._MEMORY_SOURCE_SEAL
            or operation.source._issued_id != id(operation.source) or not operation.source._live.is_set()
            or operation.source._scope is not operation.scope
            or operation.source.admission.header_budget is not guard.header_budget
            or type(prepared) is not PreparedNativeMemoryMutation
            or source_owner._MEMORY_OPERATIONS.get(id(operation))._prepared is not prepared
            or _NATIVE_MUTATION_PLANS.get(id(prepared)) is not prepared
            or type(certificate) is not CompositionHeaderCertificate or certificate.seal is not _SEAL
            or certificate.issued_id != id(certificate) or certificate.budget is not guard.header_budget
            or certificate.driver is not operation.driver
            or type(retention_widths) is not MappingProxyType
            or retained is None or retained[0] is not operation or retained[1] is not prepared
            or retained[2] is not certificate or retained[3] is not retention_widths
            or owner is None or owner[0] is not operation or owner[1] is not prepared
            or owner[2] is not certificate or owner[3] is not asyncio.current_task()
            or owner[4] is not operation.transaction
            or owner[4] is not operation.db.sync_session.get_transaction()
            or owner[5] is not operation.driver or not operation.driver.in_transaction
            or capacity is None or capacity[0] is not operation or capacity[1] is not prepared
            or capacity[2] is not certificate or type(capacity[3]) is not MappingProxyType
            or projection is None or projection[0] is not operation
            or prepared.consumed is not (operation._effect is not None)):
        raise HeaderBoundsError("memory_capture_numeric_owner_changed")
    w, facts, run_id = retention_widths, capacity[3], operation.run.run_identity
    if (any(type(amount) is not int or not 0 <= amount <= MAX_BYTES
            for name, amount in w.items() if name not in {"future_text_bytes", "completion_future_text_bytes", "claimed"})
            or type(w["future_text_bytes"]) is not MappingProxyType
            or set(w["future_text_bytes"]) != {"checkpoint_context_json", "checkpoint_receipts_json", "updated_at"}
            or any(type(name) is not str or type(amount) is not int or not 0 <= amount <= MAX_BYTES
                for name, amount in w["future_text_bytes"].items())
            or type(w.get("completion_future_text_bytes")) is not MappingProxyType
            or set(w["completion_future_text_bytes"]) != {"checkpoint_context_json", "checkpoint_receipts_json",
                "updated_at", "status", "result_digest", "result_summary", "finished_at"}
            or any(type(name) is not str or type(amount) is not int or not 0 <= amount <= MAX_BYTES
                for name, amount in w["completion_future_text_bytes"].items())
            or not 1 <= w["row_count"] <= MAX_ROWS
            or w["row_count"] != len(facts["row_uppers"])):
        raise HeaderBoundsError("memory_capture_numeric_shape_unavailable")
    def ref_upper(table, key):
        return obj({"table": width(table), "key":
            22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key)})
    def fields_upper(names):
        return array(tuple(width(name) for name in names))
    refs_tuple = array(tuple(3 + width(table) + (
        22 if type(key) is _NativeMemoryFutureIntegerSlot else width(key))
        for table, key in facts["row_uppers"]))
    locator_upper = obj({"profile": width(MEMORY_REFERENCE_PROFILE), "invocation_ref": width(run_id),
        "original_checkpoint_id": width(MEMORY_ORIGINAL_CHECKPOINT), "original_checkpoint_digest": 66})
    receipt_preimage = obj({"invocation": width(run_id),
        "claim": width(operation.scope.witness["claim_ref"]), "candidate": 66})
    sealed_upper = obj({"invocation_ref": width(run_id), "claim_ref": width(operation.scope.witness["claim_ref"]),
        "candidate_digest": 66, "result_digest": 66, "payload": w["result_upper"],
        "effect": w["effect_upper"], "retention": locator_upper})
    pending_columns = obj({"checkpoint_context_json": w["context_upper"],
        "checkpoint_receipts_json": w["journal_upper"], "revision": 20, "updated_at": 194})
    events = tuple((descriptor.table, key) for descriptor, key, _row in projection[1]
        if descriptor.table in {"audit_events", "work_board_events"})
    event_refs = array(tuple(ref_upper(table, key) for table, key in events))
    new_bounds, update_bounds, delta_stages = [], [], []
    for ordinal, (descriptor, key, row) in enumerate(projection[1]):
        address = (descriptor.table, key)
        upper, raw_upper = facts["row_uppers"][address], facts["raw_tuple_uppers"][address]
        new_bounds.append(obj({"ref": ref_upper(*address), "values": upper}))
        for stage, amount in (("row-field-set", fields_upper(descriptor.columns)),
                ("raw-codec-fields", upper), ("raw-codec-pairs", raw_upper),
                ("raw-codec-canonical", raw_upper), ("raw-codec-utf8", raw_upper),
                ("bind-map-copy", upper), ("reference", ref_upper(*address))):
            delta_stages.append((("delta-new", ordinal, stage), amount))
        for field, kind in zip(descriptor.columns, descriptor.kinds):
            if kind == "real" and row[field] is not None:
                delta_stages.append((("delta-new", ordinal, "float-hex", field), 64))
    for ordinal, (descriptor, key, changes) in enumerate(projection[2]):
        upper = _native_memory_closed_value_width(changes)
        update_bounds.append(obj({"ref": ref_upper(descriptor.table, key), "columns": upper}))
        for stage, amount in (("row-field-set", fields_upper(changes)),
                ("schema-field-set", fields_upper(descriptor.columns)),
                ("bind-map-copy", upper), ("reference", ref_upper(descriptor.table, key))):
            delta_stages.append((("delta-update", ordinal, stage), amount))
    update_bounds.append(obj({"ref": ref_upper("workflow_run_states", run_id), "columns": pending_columns}))
    delta_upper = obj({"schema_version": 1, "new_rows": array(tuple(new_bounds)),
        "updates": array(tuple(update_bounds)), "owner_events": event_refs})
    entries = []
    def add(stage, upper):
        entries.append((("native-memory-capture", run_id, stage), upper))
    for stage in ("expected-value-parser", "expected-value-pairs", "expected-value-dicts"):
        add(stage, w["effect_upper"])
    for stage in ("receipt-object", "receipt-canonical", "receipt-utf8"):
        add(stage, receipt_preimage)
    add("receipt-digest", 66)
    add("receipt-locator", width("native-memory-receipt:") + 64)
    add("expected-result", max(w["result_upper"], obj({"status": width("blocked"),
        "reason_code": width("native_memory_owner_outcome_unsupported"), "memory_status": width("no_learning")})))
    value_fields = ("proposal_ref", "revision", "state") if prepared._method == "memory.propose" else ("record_ref", "receipt_ref")
    for stage, upper in (("result-validator-map", fields_upper(("status", "value", "memory_status"))),
            ("result-outer-field-set", fields_upper(("status", "value", "memory_status"))),
            ("result-value-field-set", fields_upper(value_fields)),
            ("result-status-set", fields_upper(("succeeded",))),
            ("result-memory-status-set", fields_upper(("proposal_only", "reviewed_update", "forgotten"))),
            ("result-proposal-state-set", fields_upper(("proposed", "blocked")))):
        add(stage, upper)
    for stage in ("reference-set", "reference-sort", "reference-tuple"):
        add(stage, refs_tuple)
    add("lookup-map", refs_tuple + w["selected_rows_upper"] + sum(facts["raw_tuple_uppers"].values()))
    add("audit-hash-input", facts["raw_tuple_uppers"][("audit_events",
        prepared.audit_event.id if prepared.audit_event is not None else prepared._result["audit_event_id"])])
    add("audit-hash-return", 66)
    for stage in ("original-effect-asdict-fields", "original-effect-asdict-values",
            "original-effect-canonical", "original-effect-utf8", "sealed-effect-asdict-fields", "sealed-effect-asdict-values"):
        add(stage, w["effect_upper"])
    for stage, upper in (("original-effect-digest", 66), ("source-canonical", w["source_upper"]),
            ("source-utf8", w["source_upper"]), ("source-digest", 66), ("original-audit-reference", ref_upper("audit_events",
                prepared.audit_event.id if prepared.audit_event is not None else prepared._result["audit_event_id"])),
            ("original-audit-object", w["audit_upper"] + 66), ("original-ref-objects", w["refs_upper"]),
            ("original-payload", w["original_upper"]), ("witnesses-canonical", w["rows_upper"]),
            ("witnesses-utf8", w["rows_upper"]), ("witnesses-digest", 66),
            ("original-payload-canonical", w["original_upper"]), ("original-payload-utf8", w["original_upper"]),
            ("original-payload-digest", 66), ("original-record", w["original_record_upper"]),
            ("locator-original-canonical", w["original_record_upper"]), ("locator-original-utf8", w["original_record_upper"]),
            ("locator-original-digest", 66), ("locator-object", locator_upper),
            ("result-canonical", w["result_upper"]), ("result-utf8", w["result_upper"]), ("result-digest", 66),
            ("sealed-object", sealed_upper), ("context-copy", w["context_upper"]),
            ("context-canonical", w["context_upper"]), ("pending-map", pending_columns),
            ("append-journal-utf8", w["journal_upper"]), ("append-journal-parser", w["journal_upper"]),
            ("append-journal-pairs", w["journal_upper"]), ("append-journal-dicts", w["journal_upper"]),
            ("append-journal-rstrip", w["journal_upper"]), ("append-journal-prefix", w["journal_upper"]),
            ("append-original-canonical", w["original_record_upper"]), ("append-journal-suffix", w["journal_upper"]),
            ("append-journal-join-first", w["journal_upper"]), ("append-journal-join-second", w["journal_upper"]),
            ("append-journal-join-third", w["journal_upper"]), ("planned-row-fields", fields_upper(WRS_BY_RUN.columns)),
            ("planned-row-schema-fields", fields_upper(WRS_BY_RUN.columns)), ("planned-row", w["row_upper"]),
            ("planned-date-bind", 194), ("events-tuple", refs_tuple), ("events-sort", refs_tuple),
            ("pending-columns-copy", pending_columns), ("delta-updates-tuple", array(tuple(update_bounds))),
            ("delta-new-list", array(tuple(new_bounds))), ("delta-new-sort", refs_tuple), ("delta-new-address-set", refs_tuple),
            ("delta-update-list", array(tuple(update_bounds))), ("delta-update-sort", refs_tuple),
            ("delta-update-address-set", refs_tuple), ("delta-pending-reference", ref_upper("workflow_run_states", run_id)),
            ("delta-pending-bind-copy", pending_columns), ("delta-pending-fields", fields_upper((
                "checkpoint_context_json", "checkpoint_receipts_json", "revision", "updated_at"))),
            ("delta-pending-schema-fields", fields_upper(WRS_BY_RUN.columns)), ("delta-events-set", refs_tuple),
            ("delta-events-sort", refs_tuple), ("delta-events-objects", event_refs), ("delta-object", delta_upper),
            ("delta-validation-canonical", delta_upper), ("delta-canonical", delta_upper),
            ("delta-utf8", delta_upper), ("delta-digest", 66),
            ("current-original-canonical", w["original_record_upper"]), ("current-original-utf8", w["original_record_upper"]),
            ("current-original-digest", 66), ("current-witness-copy", w["rows_upper"]),
            ("current-own-update", w["rows_upper"]), ("current-rows-canonical", w["rows_upper"]),
            ("current-rows-utf8", w["rows_upper"]), ("current-rows-digest", 66),
            ("current-event-objects", event_refs), ("current-payload", w["current_upper"])):
        add(stage, upper)
    for stage, upper in delta_stages:
        add(stage, upper)
    for appearance, upper in _native_memory_cold_binding_digest_numeric_entries(run_id,
            w["binding_text_bytes"], owner="capture", occurrences=1):
        add(("binding-digest", appearance), upper)
    candidate_owner = getattr(source_owner, "original_native_memory_source_candidate_numeric_entries", None)
    context_owner = getattr(source_owner, "original_native_memory_source_context_numeric_entries", None)
    if not callable(candidate_owner) or not callable(context_owner):
        raise HeaderBoundsError("memory_capture_source_numeric_unavailable")
    entries.extend(candidate_owner(candidate_upper=w["source_upper"]))
    entries.extend(context_owner(context_upper=w["context_upper"], candidate_upper=w["source_upper"],
        binding_text_bytes=w["binding_text_bytes"], result_upper=w["result_upper"], effect_upper=w["effect_upper"],
        locator_upper=locator_upper, result_present=False))
    if any(type(upper) is not int or not 0 <= upper <= MAX_BYTES for _appearance, upper in entries):
        raise HeaderBoundsError("canonical_bound_not_certified")
    return tuple(entries)


async def capture_native_memory_retention(db, run, claim_ref, effect, *, source_operation, prepared, result):
    """Capture the identical returned M5 effect on its original live writer."""
    from dataclasses import asdict
    from src.runtime_plugins.memory_producer import (
        _validate_memory_owner_source, memory_owner_references, memory_context)
    from src.memory.m5 import validate_native_memory_mutation_plan
    from src.memory.header_bounds import WRS_BY_RUN, _MEMORY_LEDGER_PHASE
    from src.workflows.job_runtime import _canonical, _digest
    # Check the genuine capture phase before opaque Source readers allocate.
    from src.runtime_plugins import memory_producer as source_owner
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    if (type(source_operation) is not source_owner._MemoryOwnerSourceOperation
            or source_operation._seal is not source_owner._MEMORY_SOURCE_SEAL
            or source_operation._issued_id != id(source_operation)
            or source_owner._MEMORY_OPERATIONS.get(id(source_operation)) is not source_operation
            or source_operation.db is not db or source_operation.run is not run):
        raise NativeServiceBlocked("native_memory_actual_operation_unavailable")
    issued_source = getattr(source_operation, "source", None)
    issued_retention = _MEMORY_RETENTIONS.get(id(issued_source))
    phase = _MEMORY_LEDGER_PHASE.get()
    if (type(issued_retention) is not _NativeMemoryRetention
            or issued_retention.operation is not source_operation
            or issued_retention.prepared is not prepared
            or phase is None or phase[0] is not issued_retention.ledger or phase[1] != "capture"):
        raise ProductionWorkspaceReconciliationError("memory_retention_capture_phase_unavailable")
    issued_retention._checked()
    numeric = getattr(db.info.get("composition_guard"), "_native_memory_retention_numeric_facts", None)
    if numeric is None or numeric[0] is not source_operation or numeric[1] is not prepared:
        raise ProductionWorkspaceReconciliationError("memory_retention_capture_numeric_unavailable")
    retained_allocations = getattr(db.info.get("composition_guard"),
        "_native_memory_capture_allocation_numeric", None)
    if (type(retained_allocations) is not tuple or len(retained_allocations) != 5
            or retained_allocations[0] is not source_operation
            or retained_allocations[1] is not prepared
            or retained_allocations[2] is not numeric[2]
            or retained_allocations[3] is not numeric[3]
            or type(retained_allocations[4]) is not tuple):
        raise ProductionWorkspaceReconciliationError("memory_retention_capture_allocation_lineage_unavailable")
    # Reuse only the complete composer's original already-paid vector. No
    # numeric delta/Source/parser/helper vector is rebuilt after the M5 effect.
    allocations = retained_allocations[4]
    from src.memory.composition_headers import _metadata_size
    map_upper, selected_count = 2, 0
    for appearance, upper in allocations:
        if (type(appearance) is tuple and len(appearance) >= 3
                and appearance[0] == "native-memory-capture" and appearance[1] == run.run_identity):
            selected_count += 1
            map_upper += _metadata_size(appearance[2]) + 1 + 20
    map_upper += max(0, selected_count - 1)
    issued_source.admission.header_budget.debit(map_upper,
        appearance=("native-memory-capture", run.run_identity, "allocation-stage-map"))
    allocation_bounds = {appearance[2]: upper for appearance, upper in allocations
        if (type(appearance) is tuple and len(appearance) >= 3
                and appearance[0] == "native-memory-capture" and appearance[1] == run.run_identity)}
    def pay(*stages):
        for stage in stages:
            issued_source.admission.header_budget.debit(allocation_bounds[stage],
                appearance=("native-memory-capture", run.run_identity, stage))
    def pay_group(name):
        for stage in allocation_bounds:
            if type(stage) is tuple and stage[0] == name:
                pay(stage)
    source = await _validate_memory_owner_source(db, run, source_operation, effect=effect)
    retention = _MEMORY_RETENTIONS.get(id(source))
    if (type(retention) is not _NativeMemoryRetention or retention._checked().captured
            or retention.operation is not source_operation or retention.prepared is not prepared
            or source_operation._effect is not effect or claim_ref != source._scope.witness["claim_ref"]):
        raise ProductionWorkspaceReconciliationError("memory_retention_effect_changed")
    await validate_native_memory_mutation_plan(prepared, db=db,
        candidate_digest=source.admission.candidate_digest, consumed=True)
    await _native_memory_invalidate_affected_current(db, retention)
    from src.runtime_plugins.contracts import succeeded, blocked
    if effect.status == "succeeded":
        pay("expected-value-parser", "expected-value-pairs", "expected-value-dicts")
        expected_value = json.loads(effect.value_json)
        if effect.method != "memory.propose":
            pay("receipt-object", "receipt-canonical", "receipt-utf8", "receipt-digest", "receipt-locator")
            expected_value["receipt_ref"] = "native-memory-receipt:" + _digest({
                "invocation": run.run_identity, "claim": claim_ref,
                "candidate": source.admission.candidate_digest})
        pay("expected-result", "result-validator-map", "result-outer-field-set", "result-value-field-set",
            "result-status-set", "result-memory-status-set")
        if effect.method == "memory.propose":
            pay("result-proposal-state-set")
        expected_result = succeeded(effect.method, expected_value)
    else:
        pay("expected-result")
        expected_result = blocked("native_memory_owner_outcome_unsupported")
    if result != expected_result:
        raise ProductionWorkspaceReconciliationError("memory_retention_result_changed")
    certificate = await _native_memory_source_snapshot(db, source.admission.header_budget)
    actual_refs = await memory_owner_references(db, source.admission, effect)
    # Source locators may not expand the sealed capture allocation footprint.
    # Compare against real retained addresses before building the union set.
    if any((item.table, item.row_ref) not in retention.existing_references
            and not any(descriptor.table == item.table and key == item.row_ref
                for descriptor, key, _row in retention.new_rows)
            and not any(descriptor.table == item.table and key == item.row_ref
                for descriptor, key, _changes in retention.updates)
            and (item.table, item.row_ref) != ("audit_events", effect.audit_event_id)
            for item in actual_refs):
        raise ProductionWorkspaceReconciliationError("memory_retention_capture_footprint_changed")
    pay("reference-set", "reference-sort", "reference-tuple")
    refs = tuple(sorted({*retention.existing_references,
        *((item.table, item.row_ref) for item in actual_refs),
        *((descriptor.table, key) for descriptor, key, row in retention.new_rows),
        *((descriptor.table, key) for descriptor, key, row in retention.updates),
        ("audit_events", effect.audit_event_id)}))
    rows = await _native_memory_source_rows(db, certificate, refs)
    pay("lookup-map")
    lookup = {(descriptor.table, key): (row, encoded) for descriptor, key, row, encoded in rows}
    for descriptor, key, expected in retention.new_rows:
        if lookup[(descriptor.table, key)][0] != expected:
            raise ProductionWorkspaceReconciliationError("memory_retention_created_row_changed")
    for descriptor, key, changes in retention.updates:
        if any(lookup[(descriptor.table, key)][0][name] != value for name, value in changes.items()):
            raise ProductionWorkspaceReconciliationError("memory_retention_updated_row_changed")
    before = lookup[("workflow_run_states", run.run_identity)][0]
    if before != retention.before:
        raise ProductionWorkspaceReconciliationError("memory_retention_original_run_changed")
    witnesses = _native_memory_row_witnesses(rows, header_budget=source.admission.header_budget)
    candidate = source.admission.candidate()
    pay("audit-hash-input", "audit-hash-return")
    audit_digest = hashlib.sha256(lookup[("audit_events", effect.audit_event_id)][1]).hexdigest()
    pay_group("binding-digest")
    pay("original-effect-asdict-fields", "original-effect-asdict-values", "original-effect-canonical",
        "original-effect-utf8", "original-effect-digest", "source-canonical", "source-utf8", "source-digest",
        "original-audit-reference", "original-audit-object", "original-ref-objects", "original-payload",
        "witnesses-canonical", "witnesses-utf8", "witnesses-digest")
    original_payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "invocation_ref": run.run_identity, "claim_ref": claim_ref,
        "candidate_digest": source.admission.candidate_digest,
        "composition_binding_digest": source.claim.binding.binding_digest,
        "method": effect.method, "owner_principal_id": candidate["operator_principal_id"],
        "operator_session_id": candidate["operator_session_id"],
        "original_deadline": candidate["original_deadline"],
        "source_binding_digest": _digest(candidate.get("source")),
        "original_effect_digest": _digest(asdict(effect)),
        "original_audit": {"ref": {"table": "audit_events", "key": effect.audit_event_id},
            "tuple_digest": audit_digest},
        "refs": [{"table": table, "key": key} for table, key in refs],
        "rows_digest": _digest(witnesses), "encoded_bytes": sum(item["encoded_bytes"] for item in witnesses)}
    preflight_native_memory_reference_payload(original_payload, header_budget=source.admission.header_budget)
    pay("original-payload-canonical", "original-payload-utf8", "original-payload-digest", "original-record")
    original = {"checkpoint_id": MEMORY_ORIGINAL_CHECKPOINT, "safe": True,
        "state_digest": _digest(original_payload), "payload": original_payload}
    retention.original = original
    pay("locator-original-canonical", "locator-original-utf8", "locator-original-digest", "locator-object")
    locator = {"profile": MEMORY_REFERENCE_PROFILE, "invocation_ref": run.run_identity,
        "original_checkpoint_id": MEMORY_ORIGINAL_CHECKPOINT, "original_checkpoint_digest": _digest(original)}
    context = memory_context(run)
    pay("result-canonical", "result-utf8", "result-digest", "sealed-effect-asdict-fields",
        "sealed-effect-asdict-values", "sealed-object")
    sealed = {"invocation_ref": run.run_identity, "claim_ref": claim_ref,
        "candidate_digest": source.admission.candidate_digest, "result_digest": _digest(result),
        "payload": result, "effect": asdict(effect), "retention": locator}
    pay("context-copy", "context-canonical", "pending-map", "append-journal-utf8", "append-journal-parser",
        "append-journal-pairs", "append-journal-dicts", "append-journal-rstrip", "append-journal-prefix",
        "append-original-canonical", "append-journal-suffix", "append-journal-join-first",
        "append-journal-join-second", "append-journal-join-third")
    pending = {"checkpoint_context_json": _canonical(dict(context, result=sealed)),
        "checkpoint_receipts_json": _append_native_memory_original(before["checkpoint_receipts_json"], original),
        "revision": before["revision"] + 1, "updated_at": datetime.now(timezone.utc)}
    pay("planned-row-fields", "planned-row-schema-fields", "planned-row", "planned-date-bind")
    row = _native_memory_planned_sql_row(run, pending)
    pay("events-tuple", "events-sort")
    events = tuple(sorted((descriptor.table, key) for descriptor, key, values in retention.new_rows
        if descriptor.table in {"audit_events", "work_board_events"}))
    pay_group("delta-new")
    pay_group("delta-update")
    pay("pending-columns-copy", "delta-updates-tuple", "delta-new-list", "delta-new-sort", "delta-new-address-set",
        "delta-update-list", "delta-update-sort", "delta-update-address-set", "delta-pending-reference",
        "delta-pending-bind-copy", "delta-pending-fields", "delta-pending-schema-fields", "delta-events-set",
        "delta-events-sort", "delta-events-objects", "delta-object", "delta-validation-canonical")
    delta = _native_memory_selected_delta_preimage(retention.new_rows,
        (*retention.updates, (WRS_BY_RUN, run.run_identity,
            {name: row[name] for name in pending})), events)
    pay("delta-canonical", "delta-utf8", "delta-digest", "current-original-canonical", "current-original-utf8",
        "current-original-digest", "current-witness-copy", "current-own-update", "current-rows-canonical",
        "current-rows-utf8", "current-rows-digest", "current-event-objects", "current-payload")
    payload = {"schema_version": 2, "profile": MEMORY_REFERENCE_PROFILE,
        "original_checkpoint_digest": _digest(original), "projection_revision": 1,
        "state": "validated", "reason_code": None, "refs": original_payload["refs"],
        "absences": [], "rows": witnesses, "rows_digest": _native_memory_current_rows_digest(witnesses, run.run_identity),
        "encoded_bytes": sum(item["encoded_bytes"] for item in witnesses),
        "owner_operation_kind": effect.method,
        "owner_events": [{"table": table, "key": key} for table, key in events],
        "selected_delta_digest": _digest(delta)}
    after, current = _native_memory_prepare_current_row(row, payload, header_budget=source.admission.header_budget)
    plan = await _prepare_native_memory_source_publication(db, run, retention, before, after, certificate, "capture",
        pending_changes=pending)
    await _apply_native_memory_source_publication(db, run, plan)
    retention.captured = True
    return retention


async def validate_native_memory_retention(db, run, sealed):
    """Current cold evidence plus the still-live original Source; no reissuer."""
    from src.runtime_plugins.memory_producer import _validate_memory_lifecycle_source, memory_context
    from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, _NativeMemoryNumericLedger
    active = _MEMORY_LEDGER_PHASE.get()
    if active is None or type(active[0]) is not _NativeMemoryNumericLedger or active[1] != "completion":
        raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
    ledger = active[0]
    _native_memory_capacity_slots(ledger)
    if ledger.bindings["completion"][0] is not db:
        raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
    for stage, upper in (("registry-view", 4), ("registry-iterator", 4),
            ("matching-count", 20), ("matching-count", 20)):
        ledger.budget.debit(upper, appearance=("memory-retention-validation", stage))
    # Preserve the original uniqueness check without copying the unrelated
    # global registry into a tuple and list. At the second matching identity
    # the original len(matches)!=1 condition is already decisive.
    retention = None
    for value in _MEMORY_RETENTIONS.values():
        if value.before["run_identity"] == run.run_identity:
            if retention is not None:
                raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
            retention = value
    if retention is None:
        raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
    retention = retention._checked()
    if retention.ledger is not ledger:
        raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
    source = await _validate_memory_lifecycle_source(db, run, retention.source.claim)
    if source is not retention.source or not retention.captured or retention.completed:
        raise ProductionWorkspaceReconciliationError("memory_retention_source_changed")
    if memory_context(run)["result"] != sealed or sealed["retention"] != retention.payload():
        raise ProductionWorkspaceReconciliationError("memory_retention_result_changed")
    certificate = await _native_memory_source_snapshot(db, source.admission.header_budget)
    from src.memory.composition_headers import snapshot_reads
    def validate(connection):
        with snapshot_reads(certificate):
            _, row, _ = _native_memory_reference_row_on_connection(connection, "workflow_run_states", run.run_identity)
            return _checked_memory_reference_row(connection, row)
    await (await db.connection()).run_sync(validate)
    guard = db.info.get("composition_guard")
    if type(guard) is not CompositionSessionGuard or guard.header_budget is not source.admission.header_budget:
        raise ProductionWorkspaceReconciliationError("memory_retention_guard_changed")
    guard._native_memory_source_retention = retention
    return retention


def _native_memory_lifecycle_allocation_amounts(run_id, widths):
    """Closed successful completion construction dimensions, no future row."""
    from src.memory.header_bounds import (_native_memory_object_width as obj,
        _native_memory_array_width as array, _native_memory_ascii_string_width as width,
        _native_memory_utf8_width, WRS_BY_RUN)
    names = ("status", "result_digest", "result_summary", "failure_reason", "finished_at", "updated_at",
        "lease_owner", "lease_expires_at", "revision", "checkpoint_context_json")
    fields = array(width(name) for name in names)
    schema = array(width(name) for name in WRS_BY_RUN.columns)
    pending = obj({"status": width("succeeded"), "result_digest": 66,
        "result_summary": width("Native Memory owner result committed"), "failure_reason": 4,
        "finished_at": 194, "updated_at": 194, "lease_owner": 4, "lease_expires_at": 4,
        "revision": 20, "checkpoint_context_json": widths["context_upper"]})
    reference = obj({"table": width("workflow_run_states"), "key": width(run_id)})
    update = obj({"ref": reference, "columns": pending})
    updates = array((update,))
    address = array((3 + width("workflow_run_states") + width(run_id),))
    delta = obj({"schema_version": 1, "new_rows": 2, "updates": updates, "owner_events": 2})
    return {"allowed-set": fields, "pending-field-set": fields,
        "planned-row-fields": schema, "planned-row-schema-fields": schema,
        "planned-row": max(widths["own_upper"], widths["own_raw_upper"]), "planned-date-bind": 194,
        "reference-tuples": widths["refs_upper"], "delta-empty-input": 2,
        "delta-pending-bind-copy": pending, "delta-update-input": 5 + 4 + width(run_id) + pending,
        "delta-new-list": 2, "delta-update-list": updates,
        "delta-update-key-utf8": _native_memory_utf8_width(run_id),
        "delta-update-fields": fields, "delta-update-schema-fields": schema,
        "delta-update-bind-copy": pending, "delta-update-record": update,
        "delta-update-sort": address, "delta-update-address-set": address,
        "delta-events-set": 2, "delta-events-sort": 2, "delta-events-list": 2,
        "delta-object": delta, "delta-validation-canonical": delta,
        "delta-canonical": delta, "delta-utf8": delta, "delta-digest": 66,
        "current-payload-copy": widths["current_upper"] + widths["rows_upper"] + 20 + 66 + 2,
        "current-count-update": 20}


def _native_memory_lifecycle_allocation_numeric_entries(operation, prepared, *, certificate,
        facts, retention_widths):
    """ONE successful lifecycle constructor; rows, journal and Current separate."""
    _native_memory_generic_future_bound_inputs(operation, prepared, certificate, facts, retention_widths)
    return ((("memory-lifecycle-allocation", "numeric-map"), 2 + 28 + 29 * (6 * 32 + 2 + 1 + 20)),) + tuple((("memory-lifecycle-allocation", stage), upper) for stage, upper in
        _native_memory_lifecycle_allocation_amounts(operation.run.run_identity, retention_widths).items())


async def prepare_native_memory_lifecycle(db, run, *, original_claim, pending_changes, header_budget):
    from src.runtime_plugins.memory_producer import _validate_memory_lifecycle_source, memory_context
    from src.memory.header_bounds import WRS_BY_RUN
    from src.workflows.job_runtime import _digest
    source = await _validate_memory_lifecycle_source(db, run, original_claim)
    retention = _MEMORY_RETENTIONS.get(id(source))
    if type(retention) is not _NativeMemoryRetention or source.admission.header_budget is not header_budget:
        raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
    retention._checked()
    from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
    if _MEMORY_LEDGER_PHASE.get() != (retention.ledger, "completion"):
        raise ProductionWorkspaceReconciliationError("memory_lifecycle_numeric_owner_unavailable")
    _native_memory_capacity_slots(retention.ledger)
    lineage = getattr(retention, "_publication_numeric_facts", None)
    if (lineage is None or lineage[0] is not retention.operation or lineage[1] is not retention.prepared):
        raise ProductionWorkspaceReconciliationError("memory_lifecycle_numeric_owner_unavailable")
    # The fixed 29-field numeric map is itself a separately prepaid owner
    # construction. It holds only literal source labels and int64 capacities.
    amount_map_upper = 2 + 28 + 29 * (6 * 32 + 2 + 1 + 20)
    header_budget.debit(amount_map_upper, appearance=("memory-lifecycle-allocation", "numeric-map"))
    amounts = _native_memory_lifecycle_allocation_amounts(run.run_identity, lineage[3])
    def pay(*stages):
        for stage in stages:
            header_budget.debit(amounts[stage], appearance=("memory-lifecycle-allocation", stage))
    certificate = await _native_memory_source_snapshot(db, header_budget)
    own = await _native_memory_source_rows(db, certificate, (("workflow_run_states", run.run_identity),))
    before = own[0][2]
    original, current = preflight_native_memory_reference_journal(before["checkpoint_receipts_json"], header_budget=header_budget)
    if not retention.captured or retention.completed or original != retention.original or current is None:
        raise ProductionWorkspaceReconciliationError("memory_retention_original_changed")
    pay("allowed-set", "pending-field-set")
    allowed = {"status", "result_digest", "result_summary", "failure_reason", "finished_at", "updated_at",
        "lease_owner", "lease_expires_at", "revision", "checkpoint_context_json"}
    if set(pending_changes) != allowed:
        raise ProductionWorkspaceReconciliationError("memory_retention_completion_patch_invalid")
    pay("planned-row-fields", "planned-row-schema-fields", "planned-row", "planned-date-bind")
    row = _native_memory_planned_sql_row(run, pending_changes)
    sealed = memory_context(run)["result"]
    if (row["status"] != ("succeeded" if sealed["payload"]["status"] == "succeeded" else "blocked")
            or row["revision"] != before["revision"] + 1 or row["result_digest"] != sealed["result_digest"]
            or row["lease_owner"] is not None or row["lease_expires_at"] is not None
            or row["checkpoint_context_json"] != before["checkpoint_context_json"]
            or row["result_summary"] != "Native Memory owner result committed"
            or row["failure_reason"] != sealed["payload"].get("reason_code")
            or row["finished_at"] is None or row["updated_at"] != row["finished_at"]):
        raise ProductionWorkspaceReconciliationError("memory_retention_completion_patch_invalid")
    pay("reference-tuples")
    refs = tuple((item["table"], item["key"]) for item in current["payload"]["refs"])
    rows = await _native_memory_source_rows(db, certificate, refs)
    for stage in amounts:
        if stage.startswith("delta-"):
            pay(stage)
    pay("current-payload-copy")
    payload = dict(current["payload"], projection_revision=current["payload"]["projection_revision"] + 1,
        rows=_native_memory_row_witnesses(rows, header_budget=header_budget), selected_delta_digest=_digest(
            _native_memory_selected_delta_preimage([], [(WRS_BY_RUN, run.run_identity,
                {name: row[name] for name in pending_changes})], [])), owner_events=[])
    pay("current-count-update")
    payload["encoded_bytes"] = sum(item["encoded_bytes"] for item in payload["rows"])
    after, record = _native_memory_prepare_current_row(row, payload, header_budget=header_budget)
    return await _prepare_native_memory_source_publication(db, run, retention, before, after, certificate, "completion",
        pending_changes=pending_changes)


async def apply_native_memory_lifecycle(db, run, plan):
    if type(plan) is not _NativeMemorySourcePublication or plan.kind != "completion":
        raise ProductionWorkspaceReconciliationError("memory_publication_plan_unavailable")
    result = await _apply_native_memory_source_publication(db, run, plan)
    plan.retention._completion_publication = plan
    plan.retention.completed = True
    return result


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
    terms = [f'CASE typeof("{column}") WHEN \'null\' THEN 4 WHEN \'integer\' THEN 20 '
        f'WHEN \'real\' THEN 64 WHEN \'text\' THEN 6*octet_length("{column}")+2 ELSE -1 END'
        for column in columns]
    scalar_query = ("WITH original_metadata AS (" + query + ") SELECT count(*),"
        + "coalesce(sum(" + "+".join(terms) + "),0),coalesce(sum("
        + "+".join(f'CASE WHEN typeof("{column}") IN (\'null\',\'integer\',\'real\',\'text\') THEN 0 ELSE 1 END'
            for column in columns) + "),0) FROM original_metadata")
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


def _native_memory_cold_text_upper(value):
    from src.memory.header_bounds import _native_memory_utf8_width
    return 6 * _native_memory_utf8_width(value) + 2


def _native_memory_cold_vector_construction_upper(run_id, *, entries, groups, sizing_fields=0):
    """Closed storage ceiling for the original cold owner vector constructors.

    Each stage label has four fields. The longest literal stage in these
    owners is ``canonical-scope-projection``; the longest owner is
    ``original-current-binding``. Count each label and pair, each generated
    intermediate tuple, the growing list, and the final tuple separately.
    Sizing dictionaries have at most25 fields and keys no wider than the
    original ``accepted_memory_content_digest`` source key.
    """
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if (type(run_id) is not str or type(entries) is not int or not 0 <= entries <= 51
            or type(groups) is not int or not 1 <= groups <= 8
            or type(sizing_fields) is not int or not 0 <= sizing_fields <= 33):
        raise HeaderBoundsError("memory_cold_construction_shape_unavailable")
    label = (5 + _native_memory_ascii_string_width("native-memory-cold")
        + _native_memory_ascii_string_width("original-current-binding")
        + _native_memory_ascii_string_width(run_id)
        + _native_memory_ascii_string_width("canonical-scope-projection"))
    pair = 3 + label + 20
    # All groups together have exactly entries labels/pairs. Each group also
    # creates a generator frame and intermediate tuple; its fixed frame uses
    # the original five bound scalar arguments, not any future body value.
    frame = 2 + 2 * label + 4 * 20 + 4
    sizing_pair = 3 + _native_memory_ascii_string_width("accepted_memory_content_digest") + 20
    return (entries * (label + pair) + groups * (frame + 2)
        + entries * (_native_memory_ascii_string_width("canonical-scope-projection") + 1)
        + entries * (pair + 1) + 2
        + 2 * (2 + entries * (pair + 1))
        + sizing_fields * sizing_pair + 2 * (2 + sizing_fields * (sizing_pair + 1)))


def _native_memory_cold_vector_construction_prepay(payer, run_id, owner, *, entries, groups, sizing_fields=0):
    """Actual reader construction only; forecast callers never select a payer."""
    if payer is None:
        return
    from src.memory.composition_headers import current_budget
    from src.runtime_plugins.memory_producer import _native_memory_source_numeric_frame
    from src.memory.header_bounds import HeaderBoundsError
    if current_budget() is not payer or _native_memory_source_numeric_frame() is not payer:
        raise HeaderBoundsError("memory_cold_construction_frame_changed")
    upper = _native_memory_cold_vector_construction_upper(run_id,
        entries=entries, groups=groups, sizing_fields=sizing_fields)
    payer.debit(upper, appearance=("native-memory-cold-construction", owner, run_id))


def _native_memory_cold_binding_construction_upper(run_id, *, occurrences=1, digest_only=False):
    """Original60 constructor plus optional cold21 filtering and remapping.

    The original binding constructor roster is shared with the Source planning
    scalar:60 label/pair appends,35 nested names,23 initial sizing pairs,
    two7-field manifest inputs,6 manifest sizing pairs, and14/34/6 numeric
    generator rosters. This caller supplies the actual cold run label width.
    """
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_ascii_string_width
    if (type(run_id) is not str or type(occurrences) is not int or not 1 <= occurrences <= 2
            or type(digest_only) is not bool):
        raise HeaderBoundsError("memory_cold_construction_shape_unavailable")
    nested = 3 + len("constructor-dependencies") + 2 + len("domain-values") + 2
    name = max(nested, len("required-plus-goals-inference-set") + 2,
        4 + len("manifest") + 2 + 20 + len("origin-dependencies") + 2)
    binding_label = (5 + _native_memory_ascii_string_width("composition-binding-parser")
        + _native_memory_ascii_string_width(run_id) + name)
    binding_pair = 3 + binding_label + 20
    input_pair = 3 + name + 20
    binding = (60 * (binding_label + binding_pair) + 35 * nested
        + 2 + 2 * (2 + 60 * binding_pair + 59)
        + 23 * input_pair + (2 + 23 * input_pair + 22)
        + 2 * (7 * input_pair + (2 + 7 * input_pair + 6))
        + 6 * input_pair + (2 + 6 * input_pair + 5)
        + (2 + 14 * 20 + 13) + (2 + 34 * 20 + 33)
        + (2 + 6 * 20 + 5))
    if not digest_only:
        return binding
    label = (7 + _native_memory_ascii_string_width("native-memory-cold")
        + _native_memory_ascii_string_width("binding-digest")
        + _native_memory_ascii_string_width(run_id)
        + _native_memory_ascii_string_width("candidate") + 20 + name)
    pair = 3 + label + 20
    count = 21 * occurrences
    # One seven-name filter set, original60 once per genuine digest call,
    # retained remapped labels/pairs and both growing/final containers.
    return (occurrences * binding + 2 + 7 * (name + 1)
        + count * (label + pair) + 2 * (2 + count * (pair + 1)))


def _native_memory_cold_binding_construction_prepay(payer, run_id, *, owner, occurrences=1, digest_only=False):
    if payer is None:
        return
    from src.memory.composition_headers import current_budget
    from src.runtime_plugins.memory_producer import _native_memory_source_numeric_frame
    from src.memory.header_bounds import HeaderBoundsError
    if current_budget() is not payer or _native_memory_source_numeric_frame() is not payer:
        raise HeaderBoundsError("memory_cold_construction_frame_changed")
    payer.debit(_native_memory_cold_binding_construction_upper(run_id,
        occurrences=occurrences, digest_only=digest_only),
        appearance=("native-memory-cold-construction", owner, run_id))


def _native_memory_cold_numeric_entries(run_id, owner, upper, stages, *, _runtime_payer=None):
    """One encoded-unit upper for each original owner's named appearance."""
    from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
    if type(upper) is not int or not 0 <= upper <= MAX_BYTES:
        raise HeaderBoundsError("memory_cold_numeric_unavailable")
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, owner,
        entries=len(stages), groups=1)
    return tuple((("native-memory-cold", owner, run_id, stage),
        66 if stage.endswith("digest-return") else upper) for stage in stages)


def _native_memory_cold_prepay(entries):
    from src.memory.composition_headers import current_budget
    payer = current_budget()
    if payer is not None:
        for appearance, upper in entries:
            payer.debit(upper, appearance=appearance)


def _native_memory_cold_candidate_numeric_entries(run_id, *, row_upper,
        context_upper, arguments_upper, authority_upper, _runtime_payer=None):
    """Cold fence allocations outside the independently paid Source reader.

    memory_context owns its parser, admission/candidate, binding and result
    allocations. Direct immutable-binding parsing has its separate owner.
    """
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, "candidate",
        entries=33, groups=6, sizing_fields=4)
    entries = list(_native_memory_cold_numeric_entries(run_id, "candidate", row_upper,
        ("run-namespace", "deadline", "return-tuple")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "context", context_upper,
        ("safe-input-keys", "safe-input-sort", "safe-input-projection",
         "safe-input-canonical", "safe-input-utf8", "safe-input-digest-return")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "arguments", arguments_upper,
        ("size-utf8", "json-parser", "json-pairs", "json-dicts", "canonical")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "authority", authority_upper,
        ("size-utf8", "json-parser", "json-pairs", "json-dicts", "field-set", "grants-set", "grants-sort",
         "digest-canonical", "digest-utf8", "digest-return")))
    # Both original fingerprint objects consist of two fixed SHA strings.
    from src.memory.header_bounds import _native_memory_object_width
    fingerprint = _native_memory_object_width({"candidate": 66, "binding": 66})
    composed = _native_memory_object_width({"native_fingerprint": 66, "composition_binding_digest": 66})
    entries.extend(_native_memory_cold_numeric_entries(run_id, "fingerprint", fingerprint,
        ("object", "canonical", "utf8", "digest-return", "spec-namespace")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "composition-fingerprint", composed,
        ("object", "canonical", "utf8", "digest-return")))
    return tuple(entries)


def _native_memory_cold_claim_numeric_entries(run_id, journal_upper, *, claimed, _runtime_payer=None):
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, "claim",
        entries=17 if claimed else 6, groups=2 if claimed else 1)
    entries = list(_native_memory_cold_numeric_entries(run_id, "claim", journal_upper,
        ("journal-size-utf8", "journal-parser", "journal-pairs", "journal-dicts", "claims-list", "checkpoint-strings")))
    if claimed:
        entries.extend(_native_memory_cold_numeric_entries(run_id, "claim", journal_upper,
            ("record-field-set", "witness-payload-copy", "witness-closed-copy", "witness-field-set",
             "witness-canonical", "witness-utf8", "witness-digest-return", "methods-set", "methods-sort",
             "witness-return-copy", "methods-tuple")))
    return tuple(entries)


def _native_memory_cold_binding_digest_numeric_entries(run_id, text_bytes, *, owner, occurrences, _runtime_payer=None):
    """The existing binding payload/manifest grammar for actual digest calls."""
    from src.workflows.inference_accounting import _original_native_binding_numeric_entries
    if owner == "candidate":
        constructor_owner = "binding-digest-candidate"
    elif owner == "claim":
        constructor_owner = "binding-digest-claim"
    elif owner == "original":
        constructor_owner = "binding-digest-original"
    elif owner == "capture":
        constructor_owner = "binding-digest-capture"
    else:
        from src.memory.header_bounds import HeaderBoundsError
        raise HeaderBoundsError("memory_cold_construction_shape_unavailable")
    _native_memory_cold_binding_construction_prepay(_runtime_payer, run_id,
        owner=constructor_owner, occurrences=occurrences, digest_only=True)
    payload_stages = {"payload-methods-list", "payload-dependencies-list", "binding-payload", "binding-canonical",
        "binding-encoded", "binding-digest", "dependency-payloads"}
    entries = []
    for occurrence in range(occurrences):
        for appearance, upper in _original_native_binding_numeric_entries(run_id, text_bytes):
            stage = appearance[2]
            if stage in payload_stages or (type(stage) is tuple and stage[:2] == ("manifest", 1)):
                entries.append((("native-memory-cold", "binding-digest", run_id, owner, occurrence, stage), upper))
    return tuple(entries)


def _checked_memory_candidate_row(row):
    """Original input encodings and immutable binding, without a Source grant."""
    from src.runtime_plugins.memory_producer import memory_context
    from src.runtime_plugins.dispatch import _receipt_witness, _ms
    from src.runtime_plugins.ownership import RuntimeCompositionBinding
    from src.memory.header_bounds import HeaderBoundsError, strict_json_loads
    from src.workflows.job_runtime import _canonical, _digest, _safe_durable_inputs, _composition_fingerprint
    try:
        from src.memory.composition_headers import current_budget
        payer = current_budget()
        if payer is not None:
            _native_memory_cold_prepay(_native_memory_cold_candidate_numeric_entries(row["run_identity"],
                row_upper=_native_memory_closed_value_width(row),
                context_upper=_native_memory_cold_text_upper(row["checkpoint_context_json"]),
                arguments_upper=_native_memory_cold_text_upper(row["arguments_json"]),
                authority_upper=_native_memory_cold_text_upper(row["declared_authority_json"]), _runtime_payer=current_budget()))
        # Source memory_context pays its own binding parse and digest. Only
        # the two subsequent direct fingerprint digest calls belong here.
        from src.memory.composition_headers import current_budget
        payer = current_budget()
        if payer is not None:
            from src.memory.header_bounds import _native_memory_utf8_width
            _native_memory_cold_prepay(_native_memory_cold_binding_digest_numeric_entries(row["run_identity"],
                _native_memory_utf8_width(row["composition_binding_json"]), owner="candidate", occurrences=2, _runtime_payer=current_budget()))
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
            _native_memory_cold_binding_construction_prepay(payer, row["run_identity"], owner="binding-parser")
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
        from src.memory.composition_headers import current_budget
        if current_budget() is not None:
            _native_memory_cold_prepay(_native_memory_cold_claim_numeric_entries(row["run_identity"],
                _native_memory_cold_text_upper(row["checkpoint_receipts_json"]), claimed=False, _runtime_payer=current_budget()))
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
            if current_budget() is not None:
                _native_memory_cold_prepay(_native_memory_cold_numeric_entries(row["run_identity"], "claim",
                    _native_memory_closed_value_width(claims[0]),
                    ("record-field-set", "witness-payload-copy", "witness-closed-copy", "witness-field-set",
                     "witness-canonical", "witness-utf8", "witness-digest-return", "methods-set", "methods-sort",
                     "witness-return-copy", "methods-tuple"), _runtime_payer=current_budget()))
            if set(claims[0]) != {"checkpoint_id", "state_digest", "safe", "payload"}:
                raise ValueError("memory_preoriginal_claim_changed")
            claim = _receipt_witness(claims[0])
            from src.memory.composition_headers import current_budget
            payer = current_budget()
            if payer is not None:
                from src.workflows.inference_accounting import _original_native_binding_numeric_entries, _accounting_prepay
                original_text = row["composition_binding_json"]
                text_bytes = sum(1 if ord(c) < 128 else 2 if ord(c) < 2048 else 3 if ord(c) < 65536 else 4 for c in original_text)
                _native_memory_cold_binding_construction_prepay(payer, row["run_identity"], owner="binding-parser")
                _accounting_prepay(payer, _original_native_binding_numeric_entries(row["run_identity"], text_bytes))
                _native_memory_cold_prepay(_native_memory_cold_binding_digest_numeric_entries(row["run_identity"],
                    text_bytes, owner="claim", occurrences=1, _runtime_payer=current_budget()))
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
                ):
            raise ValueError("memory_preoriginal_state_changed")
        # This original parser executes only after the preceding shape checks.
        # Charge its actual input before decoding; the genuine baseline trace
        # then carries this appearance into the original BODY forecast.
        from src.memory.composition_headers import current_budget
        payer = current_budget()
        if payer is not None:
            payer.debit(_native_memory_cold_text_upper(row["effect_receipts_json"]),
                appearance=("native-memory-cold", "preoriginal", row["run_identity"],
                    "effect-receipts-json"))
        if strict_json_loads(row["effect_receipts_json"]) != []:
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
            or payload["original_checkpoint_digest"] is not None or context["result"] is not None):
        raise ValueError("memory_unknown_unverified_shape_changed")
    from src.memory.composition_headers import current_budget
    payer = current_budget()
    if payer is not None:
        payer.debit(_native_memory_cold_text_upper(row["effect_receipts_json"]),
            appearance=("native-memory-cold", "unknown-unverified", row["run_identity"],
                "effect-receipts-json"))
    if (strict_json_loads(row["effect_receipts_json"]) != []
            or row["status"] not in {"cancelled", "failed", "blocked", "unknown_external_effect", "cost_liability"}
            or payload["owner_events"] != [] or payload["absences"] != []):
        raise ValueError("memory_unknown_unverified_shape_changed")
    _checked_memory_claim_row(row, run, context, binding, unverified_unknown=True)
    return context["candidate"], None


def _native_memory_reference_row_on_connection(connection, table, key):
    """Read one current certified raw reference tuple, never Source authority."""
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS, MEMORY_DESCRIPTORS, HeaderBoundsError
    from src.memory.composition_headers import charge_row, current_budget, _CURRENT
    descriptor = COMPOSITION_DESCRIPTORS.get(table)
    if (current_budget() is None or descriptor is None
            or table not in {*RETAINED_FIELDS, *MEMORY_DESCRIPTORS}):
        raise HeaderBoundsError("memory_current_bound_not_certified")
    charge_row(connection, table, key)
    certificate = _CURRENT.get()
    header_key = int(_composition_address(table, key)) if table == "work_board_events" else key
    upper = certificate.rows[(table, header_key)][1]
    # SQL delivery, the named mapping, canonical text and encoded return are
    # distinct appearances on the same original certificate/frame.
    for appearance in ("mapping", "canonical", "encoded-return"):
        certificate.budget.debit(upper, appearance=("memory-reference-output", appearance, table, key))
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


def _native_memory_cold_original_numeric_entries(run_id, *, effect_upper, source_upper, original_upper, _runtime_payer=None):
    from src.memory.header_bounds import _native_memory_object_width, _native_memory_ascii_string_width
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, "original",
        entries=21, groups=5, sizing_fields=2)
    entries = list(_native_memory_cold_numeric_entries(run_id, "original-effect", effect_upper,
        ("kwargs-copy", "construction", "field-set", "typed-field-set", "asdict-field-copy", "asdict-value-copy",
         "digest-asdict-copy", "digest-asdict-value-copy", "digest-canonical", "digest-utf8", "digest-return")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "original-source", source_upper,
        ("digest-canonical", "digest-utf8", "digest-return")))
    for owner in ("original-current-binding", "original-result-binding"):
        entries.extend(_native_memory_cold_numeric_entries(run_id, owner, original_upper,
            ("digest-canonical", "digest-utf8", "digest-return")))
    # The already selected Effect encoding bounds its retained audit key.
    audit_ref = _native_memory_object_width({"table": _native_memory_ascii_string_width("audit_events"),
        "key": effect_upper})
    entries.extend(_native_memory_cold_numeric_entries(run_id, "original-audit", audit_ref, ("reference",)))
    return tuple(entries)


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
    from src.memory.composition_headers import current_budget
    if current_budget() is not None:
        _native_memory_cold_prepay(_native_memory_cold_original_numeric_entries(row["run_identity"],
            effect_upper=_native_memory_closed_value_width(result["effect"]),
            source_upper=_native_memory_closed_value_width(candidate.get("source")),
            original_upper=_native_memory_closed_value_width(original), _runtime_payer=current_budget()))
        from src.memory.header_bounds import _native_memory_utf8_width
        _native_memory_cold_prepay(_native_memory_cold_binding_digest_numeric_entries(row["run_identity"],
            _native_memory_utf8_width(row["composition_binding_json"]), owner="original", occurrences=2, _runtime_payer=current_budget()))
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


def _native_memory_cold_reference_numeric_entries(run_id, *, rows_upper, row_count,
        selected_rows_upper, own_upper, own_journal_upper, _runtime_payer=None):
    """Current aggregate, own tuple and output appearances, excluding reads.

    Reference delivery and shared journal/projection parsers have their own
    owner schedules. The historical unlabelled own-body debit is separate.
    """
    from src.memory.header_bounds import (
        HeaderBoundsError, MAX_ROWS, _native_memory_object_width, _native_memory_ascii_string_width)
    if type(row_count) is not int or not 1 <= row_count <= MAX_ROWS:
        raise HeaderBoundsError("memory_cold_numeric_unavailable")
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, "reference",
        entries=21, groups=8, sizing_fields=2)
    own_ref = _native_memory_object_width({"table": _native_memory_ascii_string_width("workflow_run_states"),
        "key": _native_memory_ascii_string_width(run_id)})
    entries = list(_native_memory_cold_numeric_entries(run_id, "current-reference", own_ref, ("own-reference",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-aggregate", rows_upper,
        ("normalized-list", "normalized-row-copies", "normalized-own-update", "digest-canonical", "digest-utf8", "digest-return")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-aggregate", own_ref * row_count,
        ("own-reference-comparisons",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-row-hash", 66 * row_count,
        ("digest-outputs",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-own-row", own_upper,
        ("raw-codec-fields", "raw-codec-container", "raw-codec-canonical", "raw-codec-utf8",
         "projection-row-copy", "projected-codec-fields", "projected-codec-container",
         "projected-codec-canonical", "projected-codec-utf8")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-own-row", own_journal_upper,
        ("projection-kwargs",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-reference", selected_rows_upper + rows_upper,
        ("raw-rows-map",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "current-reference", rows_upper, ("raw-row-key-tuples",)))
    return tuple(entries)


def _native_memory_cold_audit_numeric_entries(run_id, *, audit_upper, details_upper, _runtime_payer=None):
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, "audit",
        entries=6, groups=3, sizing_fields=5)
    entries = list(_native_memory_cold_numeric_entries(run_id, "audit", audit_upper,
        ("tuple-digest-return",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "audit-details", details_upper,
        ("size-utf8", "json-parser", "json-pairs", "json-dicts")))
    expected = _native_memory_closed_value_width([
        "memory_learning_accepted", "memory_corrected", "memory_learning_rejected", "memory_learning_expired",
        "memory_learning_rolled_back"])
    entries.extend(_native_memory_cold_numeric_entries(run_id, "audit", expected, ("expected-event-types",)))
    return tuple(entries)


def _native_memory_cold_receipt_numeric_entries(run_id, *, receipt_upper, proposal_upper, evidence_upper, _runtime_payer=None):
    """The two original receipt checks, including their nested source projection.

    Original raw rows bound retained values; the literal envelopes below bound
    added keys/defaults and normalized timestamp strings. This constructs no
    MAC payload and supplies neither the signing key nor verification result.
    """
    from src.memory.header_bounds import _native_memory_object_width, _native_memory_ascii_string_width
    from src.memory.m5 import M5_NONE_DIGEST, M5_NONE_MEMORY, M5_NONE_PROPOSAL, M5_RECEIPT_SCHEMA_VERSION
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, "receipt",
        entries=21, groups=5, sizing_fields=33)
    source_fields = ("owner_principal_id", "owner_session_id", "source_task_id", "source_task_revision",
        "source_attempt_id", "source_attempt_fence", "workflow_run_id", "workflow_run_revision", "goal_id",
        "goal_revision", "capability_id", "capability_version", "typed_input_digest", "source_context_digest",
        "candidate_set_digest", "evidence_digest", "readback_kind", "readback_ref", "readback_digest",
        "artifact_ref", "artifact_digest", "proposal_job_id", "request_idempotency_key", "request_binding_digest",
        "source_evidence_ids")
    source_upper = proposal_upper + _native_memory_object_width({name: 4 for name in source_fields})
    mac_upper = receipt_upper + _native_memory_object_width({
        "version": _native_memory_ascii_string_width("work-board-m5-receipt-integrity.v1"), "receipt": 4}) + 4 * 34
    binding_upper = receipt_upper + proposal_upper + _native_memory_object_width({
        "version": _native_memory_ascii_string_width(M5_RECEIPT_SCHEMA_VERSION), "stage": 4, "status": 4,
        "accepted_proposal_id": _native_memory_ascii_string_width(M5_NONE_PROPOSAL),
        "accepted_memory_id": _native_memory_ascii_string_width(M5_NONE_MEMORY),
        "accepted_memory_content_digest": _native_memory_ascii_string_width(M5_NONE_DIGEST)})
    entries = list(_native_memory_cold_numeric_entries(run_id, "receipt-integrity", mac_upper,
        ("payload", "retrieval-evidence-parser", "timestamp-normalization", "envelope",
         "canonical-copy", "canonical-sort", "canonical-json", "canonical-utf8", "digest-return")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "receipt-source", source_upper,
        ("normalized-source", "source-evidence-copy", "source-strings")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "receipt-source", proposal_upper,
        ("proposal-evidence-parser",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "receipt-source", evidence_upper,
        ("receipt-evidence-parser",)))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "receipt-binding", binding_upper,
        ("field-normalization", "payload", "canonical-copy", "canonical-sort", "canonical-json", "canonical-utf8",
         "digest-return")))
    return tuple(entries)


def _native_memory_cold_selection_numeric_entries(run_id, *, memory_upper, proposal_upper, metadata_upper, scope_upper, _runtime_payer=None):
    """Original accepted-selection verification and all repeated projections."""
    from src.memory.header_bounds import _native_memory_object_width, _native_memory_ascii_string_width
    from src.memory.repository import _M5_SELECTION_BINDING_SCHEMA_VERSION
    _native_memory_cold_vector_construction_prepay(_runtime_payer, run_id, "selection",
        entries=51, groups=3, sizing_fields=11)
    envelope = _native_memory_object_width({name: 4 for name in (
        "proposal_id", "accepted_content_digest", "owner_principal_id", "owner_session_id", "source_context_digest",
        "verified_source_binding", "decision_effect", "memory_scope", "correction_target",
        "recovered_from_proposal_id", "lifecycle_state")})
    upper = memory_upper + proposal_upper + envelope + _native_memory_ascii_string_width(_M5_SELECTION_BINDING_SCHEMA_VERSION)
    entries = list(_native_memory_cold_numeric_entries(run_id, "selection", metadata_upper,
        ("metadata-size-utf8", "metadata-parser", "metadata-pairs", "metadata-dicts")))
    entries.extend(_native_memory_cold_numeric_entries(run_id, "selection", scope_upper,
        ("scope-size-utf8", "scope-parser", "scope-pairs", "scope-dicts")))
    # Matches() projects expected and canonical scope/source once; its MAC
    # owner projects the canonical values once more. Every call remains paid.
    entries.extend(_native_memory_cold_numeric_entries(run_id, "selection", upper,
        ("text-newlines-crlf", "text-newlines-cr", "text-nfc", "text-strip", "text-utf8", "text-digest-return",
         "expected-scope", "expected-scope-list", "expected-scope-model", "expected-scope-projection",
         "canonical-scope", "canonical-scope-list", "canonical-scope-model", "canonical-scope-projection",
         "expected-source-parser", "expected-source", "expected-source-list", "expected-source-strings",
         "canonical-source", "canonical-source-list", "canonical-source-strings",
         "correction", "correction-strings", "recovery-strings", "key-id-utf8", "key-id-digest-return",
         "mac-scope", "mac-scope-list", "mac-scope-model", "mac-scope-projection",
         "mac-source", "mac-source-list", "mac-source-strings", "mac-correction-input", "mac-correction",
         "mac-correction-strings", "mac-recovery-strings", "mac-payload", "mac-canonical-copy", "mac-canonical-sort",
         "mac-canonical-json", "mac-canonical-utf8", "mac-digest-return")))
    return tuple(entries)


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
            _native_memory_cold_prepay(_native_memory_cold_numeric_entries(row["run_identity"], "current-output",
                _native_memory_cold_text_upper(row["checkpoint_context_json"]) + 7, ("result-tuple",), _runtime_payer=current_budget()))
            return _checked_preoriginal_memory_row(row), None
        if current is None:
            raise ValueError("memory_current_projection_missing")
        candidate, payload = context["candidate"], current["payload"]
        if ((original is None or payload["state"] != "unknown")
                and payload["owner_operation_kind"] != candidate["method"]):
            raise ValueError("memory_current_operation_changed")
        from src.memory.header_bounds import _native_memory_object_width, _native_memory_ascii_string_width
        _native_memory_cold_vector_construction_prepay(budget, row["run_identity"], "own-reference-sizing",
            entries=0, groups=1, sizing_fields=2)
        own_ref_upper = _native_memory_object_width({"table": _native_memory_ascii_string_width("workflow_run_states"),
            "key": _native_memory_ascii_string_width(row["run_identity"])})
        _native_memory_cold_prepay(_native_memory_cold_numeric_entries(row["run_identity"],
            "current-reference", own_ref_upper, ("own-reference",), _runtime_payer=current_budget()))
        own_ref = {"table": "workflow_run_states", "key": row["run_identity"]}
        if own_ref not in payload["refs"]:
            raise ValueError("memory_current_own_reference_missing")
        if original is None:
            _native_memory_cold_prepay(_native_memory_cold_numeric_entries(row["run_identity"], "current-output",
                _native_memory_closed_value_width(candidate) + 7, ("result-tuple",), _runtime_payer=current_budget()))
            return _checked_memory_unknown_unverified_row(row, run, context, binding, current)
        effect, audit_ref = _checked_memory_original_bindings(row, run, context, binding, original, current)
        if payload["state"] == "unknown":
            # The fixed parser and immutable native bindings permit retaining
            # opaque uncertainty. They do not authenticate the old M5 effect,
            # an intervening owner, or a historical CAS. In particular, the
            # closed operation value can describe a different later M5 owner.
            # Only the validated branch below reports verified audit evidence.
            _native_memory_cold_prepay(_native_memory_cold_numeric_entries(row["run_identity"], "current-output",
                _native_memory_closed_value_width(candidate) + 7, ("result-tuple",), _runtime_payer=current_budget()))
            return candidate, None
        _, audit, audit_bytes = _native_memory_reference_row_on_connection(connection, "audit_events", effect.audit_event_id)
        _native_memory_cold_prepay(_native_memory_cold_audit_numeric_entries(row["run_identity"],
            audit_upper=_native_memory_closed_value_width(audit),
            details_upper=_native_memory_cold_text_upper(audit["details_json"]), _runtime_payer=current_budget()))
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
        from src.memory.composition_headers import _CURRENT
        own_upper = _CURRENT.get().rows[("workflow_run_states", row["run_identity"])][1]
        reference_entries = _native_memory_cold_reference_numeric_entries(row["run_identity"],
            rows_upper=_native_memory_closed_value_width(payload["rows"]), row_count=len(payload["rows"]),
            selected_rows_upper=sum(_CURRENT.get().rows[(item["ref"]["table"],
                int(item["ref"]["key"]) if item["ref"]["table"] == "work_board_events"
                else item["ref"]["key"])][1]
                for item in payload["rows"]),
            own_upper=own_upper, own_journal_upper=_native_memory_cold_text_upper(row["checkpoint_receipts_json"]), _runtime_payer=current_budget())
        # own-reference was constructed and prepaid before the Unknown branch.
        _native_memory_cold_vector_construction_prepay(budget, row["run_identity"], "reference-slice",
            entries=20, groups=1)
        _native_memory_cold_prepay(reference_entries[1:])
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
                certificate = _CURRENT.get()
                _native_memory_cold_prepay(_native_memory_cold_receipt_numeric_entries(row["run_identity"],
                    receipt_upper=certificate.rows[(table, key)][1],
                    proposal_upper=0 if proposal is None else certificate.rows[("memory_proposals", actual["source_proposal_id"])][1],
                    evidence_upper=_native_memory_cold_text_upper(actual["retrieval_evidence_ids_json"]), _runtime_payer=current_budget()))
                if not _m5_receipt_integrity_matches(actual) or not _m5_receipt_binding_matches(actual, proposal):
                    raise ValueError("memory_current_mac_changed")
            if table == "memory_proposals" and actual["status"] == "accepted":
                memory = raw_rows.get(("memories", actual["accepted_memory_id"]))
                if memory is None:
                    raise ValueError("memory_current_accepted_memory_missing")
                from src.memory.m5 import m5_text_digest
                certificate = _CURRENT.get()
                _native_memory_cold_prepay(_native_memory_cold_selection_numeric_entries(row["run_identity"],
                    memory_upper=certificate.rows[("memories", actual["accepted_memory_id"])][1],
                    proposal_upper=certificate.rows[(table, key)][1],
                    metadata_upper=_native_memory_cold_text_upper(memory["metadata_json"]),
                    scope_upper=_native_memory_cold_text_upper(actual["memory_scope_json"]), _runtime_payer=current_budget()))
                provenance = strict_json_loads(memory["metadata_json"]).get("work_board_provenance")
                if (m5_text_digest(memory["content"]) != actual["accepted_memory_content_digest"]
                        or not _m5_selection_binding_matches(provenance, proposal_id=actual["proposal_id"],
                            accepted_content_digest=actual["accepted_memory_content_digest"],
                            decision_effect=actual["decision_effect"], memory_scope=strict_json_loads(actual["memory_scope_json"]),
                            source_binding=actual, corrects_memory_id=actual["corrects_memory_id"],
                            recovered_from_proposal_id=actual["recovered_from_proposal_id"])):
                    raise ValueError("memory_current_mac_changed")
        _native_memory_cold_prepay(_native_memory_cold_numeric_entries(row["run_identity"], "current-output",
            _native_memory_closed_value_width(candidate) + _native_memory_closed_value_width(effect.audit_event_id) + 66 + 6,
            ("audit-tuple", "result-tuple"), _runtime_payer=current_budget()))
        _record_native_memory_retained_current(connection, row, original, current)
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

def _native_memory_private_journal_numeric_entries(operation, prepared, *, certificate,
        retention_widths, kind, affected_proof=None):
    """ONE visible journal CAS vector; sibling owner forecasts remain separate.

    Only already paid Source rows supply branch facts. Unsupported sibling
    checkpoints stop this forecast before the first Memory effect.
    """
    import asyncio
    from types import MappingProxyType
    from src.memory.header_bounds import (HeaderBoundsError, MAX_BYTES,
        _native_memory_utf8_width, _native_memory_ascii_string_width)
    from src.runtime_plugins.memory_producer import _MEMORY_OPERATIONS, _MEMORY_SOURCES
    if _MEMORY_OPERATIONS.get(id(operation)) is not operation:
        raise HeaderBoundsError("memory_private_journal_owner_unavailable")
    source = operation.source
    guard = operation.db.info.get("composition_guard")
    retention = getattr(source, "_retention", None)
    facts = (getattr(retention, "_publication_numeric_facts", None) if retention is not None
        else getattr(guard, "_native_memory_retention_numeric_facts", None))
    owner = getattr(guard, "_native_memory_source_capacity_owner", None)
    if (_MEMORY_SOURCES.get(id(source)) is not source or not source._live.is_set()
            or operation._prepared is not prepared or type(guard) is not CompositionSessionGuard
            or guard.db is not operation.db or source.admission.header_budget is not guard.header_budget
            or asyncio.current_task() is None
            or not ((type(owner) is tuple and len(owner) == 6 and owner[0] is operation
                and owner[1] is prepared and owner[2] is certificate and owner[3] is asyncio.current_task())
                or source._owner_task is asyncio.current_task())
            or type(facts) is not tuple or len(facts) != 4 or facts[0] is not operation
            or facts[1] is not prepared or facts[2] is not certificate
            or certificate.budget is not guard.header_budget
            or certificate.metadata_budget is not guard.header_budget
            or type(retention_widths) is not MappingProxyType
            or kind not in {"capture", "completion", "affected-unknown"}):
        raise HeaderBoundsError("memory_private_journal_owner_unavailable")
    if retention is not None:
        retention._checked()
    if kind == "affected-unknown":
        affected = (getattr(retention, "_affected_publication_numeric_facts", None) if retention is not None
            else getattr(guard, "_native_memory_affected_numeric_facts", None))
        if (type(affected) is not tuple or len(affected) != 5 or affected[0] is not operation
                or affected[1] is not prepared or affected[2] is not certificate
                or type(affected[4]) is not MappingProxyType
                or type(affected_proof) is not _NativeMemoryRetainedCurrentProof
                or not any(value is affected_proof for value in affected[3])
                or affected_proof.operation is not operation
                or affected[4].get(affected_proof.before['run_identity']) is not retention_widths):
            raise HeaderBoundsError("memory_private_journal_owner_unavailable")
        before = affected_proof.before
    else:
        if facts[3] is not retention_widths:
            raise HeaderBoundsError("memory_private_journal_owner_unavailable")
        if retention is not None:
            before = retention.before
        else:
            selected = getattr(guard, "_native_memory_existing_source_rows", None)
            if type(selected) is not tuple or selected[0] is not operation:
                raise HeaderBoundsError("memory_private_journal_owner_unavailable")
            before = None
            for descriptor, key, row in selected[2]:
                if descriptor.table == "workflow_run_states" and key == operation.run.run_identity:
                    if before is not None:
                        raise HeaderBoundsError("memory_private_journal_owner_unavailable")
                    before = row
            if before is None:
                raise HeaderBoundsError("memory_private_journal_owner_unavailable")
    old = 6 * _native_memory_utf8_width(before['checkpoint_receipts_json'] or '[]') + 2
    future = retention_widths.get('journal_upper')
    if type(future) is not int or not 0 <= future <= MAX_BYTES or old > MAX_BYTES:
        raise HeaderBoundsError("memory_private_journal_bound_unavailable")
    bound = max(old, future)
    # This paid original-row parse is a forecast observation, never a new body
    # or writer grant. The same appearance is retained in the invocation vector.
    guard.header_budget.debit(old, appearance=('memory-private-journal', kind, 'source-branch-parse'))
    records = json.loads(before['checkpoint_receipts_json'] or '[]')
    if type(records) is not list:
        raise HeaderBoundsError("memory_private_journal_bound_unavailable")
    for item in records:
        identifier = item.get('checkpoint_id') if type(item) is dict else None
        if type(identifier) is str and (identifier.startswith('runtime-service-invocation:task-capability:')
                or identifier.startswith('runtime-service-invocation:programme-start:')
                or identifier in {'conversation:cancel', 'conversation:callback-closure',
                    'conversation:operation-family', 'inference:owned-output-intent.v1',
                    'inference:owned-output.v1', 'inference:original-candidate.v1'}):
            raise HeaderBoundsError("memory_private_journal_sibling_schedule_unavailable")
    permission = 2 + 2 * bound + 1
    for name in ('composition_native_claim_receipt', 'composition_native_turn_receipt',
            'composition_native_turn_family_receipt'):
        permission += _native_memory_closed_value_width(operation.db.info.get(name)) + 1
    stages = ('source-branch-parse', 'old-json', 'new-json', 'old-selected-map', 'new-selected-map',
        'old-checkpoint-text', 'new-checkpoint-text', 'memory-permission-tuple', 'task-id-strings',
        'memory-id-set', 'task-id-set', 'task-intersection', 'permission-tuple',
        'programme-key-difference', 'control-id-set', 'control-intersection', 'output-id-set',
        'selected-pair-tuple', 'old-items-output', 'new-key-difference',
        'numeric-amounts-tuple', 'numeric-vector')
    task_strings = sum(_native_memory_ascii_string_width('runtime-service-invocation:task-capability:')
        + _native_memory_ascii_string_width(phase) - 2
        for phase in ('admission', 'source', 'invoke', 'outcome', 'cleanup'))
    amounts_upper = 2 + 7 * len(stages) + len(stages) - 1
    vector_upper = 3 * (2 + len(stages) - 1 + sum(2 + 2
        + _native_memory_ascii_string_width('memory-private-journal') + 1
        + _native_memory_ascii_string_width(kind) + 1
        + _native_memory_ascii_string_width(stage) + 1 + 7 for stage in stages))
    guard.header_budget.debit(amounts_upper,
        appearance=('memory-private-journal', kind, 'numeric-amounts-tuple'))
    guard.header_budget.debit(vector_upper,
        appearance=('memory-private-journal', kind, 'numeric-vector'))
    # Each selected key occurs in its original receipt and as the map key;
    # twice the complete journal therefore bounds the selected map/output.
    amounts = (old, bound, bound, 2 * bound + 2, 2 * bound + 2,
        2 * bound + 2, 2 * bound + 2, 2 * bound + 3, task_strings,
        3 + _native_memory_ascii_string_width(MEMORY_ORIGINAL_CHECKPOINT)
            + _native_memory_ascii_string_width(MEMORY_CURRENT_CHECKPOINT),
        2 + task_strings + 4,
        2 * bound + 2, permission, 4 * bound + 3,
        _native_memory_closed_value_width(('conversation:cancel', 'conversation:callback-closure')),
        2 * bound + 2,
        _native_memory_closed_value_width(('inference:owned-output-intent.v1',
            'inference:owned-output.v1', 'inference:original-candidate.v1')),
        4 * bound + 3, 2 * bound + 2, 4 * bound + 3, amounts_upper, vector_upper)
    if any(amount > MAX_BYTES for amount in amounts):
        raise HeaderBoundsError("memory_private_journal_bound_unavailable")
    return tuple((('memory-private-journal', kind, stage), amount)
        for stage, amount in zip(stages, amounts))


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

    async def _native_memory_publication_numeric_widths(self, operation, prepared, *, certificate, facts, retention_widths):
        """Original writer's private publication capacities, never target facts."""
        from types import MappingProxyType
        from src.memory.header_bounds import HeaderBoundsError, _native_memory_object_width as obj, _native_memory_array_width as array
        from src.workspace.production import (_publication_json_upper as width,
            MAX_ACCOUNTING_CHECKPOINT_BYTES, MAX_LIFECYCLE_RECEIPT_BYTES)
        guard, _rows, own, audit, _new, _candidate = _native_memory_generic_future_bound_inputs(
            operation, prepared, certificate, facts, retention_widths)
        if (guard is not self or type(self.base) is not dict or self.base.get("schema_version") != 2
                or type(self.base.get("native")) is not dict):
            raise HeaderBoundsError("memory_publication_numeric_shape_unavailable")
        native = self.base["native"]
        if audit in self.members:
            raise HeaderBoundsError("memory_publication_numeric_graph_unavailable")
        self.header_budget.debit(width(native), appearance=("memory-publication-width", "native-numeric-map"))
        counts = dict(native["table_counts"])
        counts["audit_events"] = counts.get("audit_events", 0) + 1
        self.header_budget.debit(width(native["relation_counts"]) + width("audit_event_session_fk") + 20,
            appearance=("memory-publication-width", "relation-numeric-map"))
        relations = dict(native["relation_counts"])
        relations["audit_event_session_fk"] = relations.get("audit_event_session_fk", 0) + 1
        self.header_budget.debit(2 + sum(width(key) + 21 for key in native) + max(0, len(native) - 1),
            appearance=("memory-publication-width", "native-field-width-map"))
        native_width = obj({key: (width(native["total_rows"] + 1) if key == "total_rows"
            else width(counts) if key == "table_counts" else width(relations) if key == "relation_counts"
            else width(value)) for key, value in native.items()})
        self.header_budget.debit(2 + sum(width(key) + 21 for key in self.base) + max(0, len(self.base) - 1),
            appearance=("memory-publication-width", "target-field-width-map"))
        target_width = obj({key: native_width if key == "native" else width(value)
            for key, value in self.base.items()})
        # Only genuine existing member changes enter the original generic delta.
        projection = self._native_memory_source_projection
        affected = self._native_memory_affected_numeric_facts[4]
        members_upper = array(width(item) for item in self.members)
        self.header_budget.debit(members_upper + width(audit) + width(own),
            appearance=("memory-publication-width", "member-key-projection"))
        changed = {own, audit}
        changed.update((descriptor.table, key) for descriptor, key, _changes in projection[2]
            if (descriptor.table, key) in self.members)
        changed.update(("workflow_run_states", key) for key in affected
            if ("workflow_run_states", key) in self.members)
        self.header_budget.debit(members_upper + 2 * width(audit) + 4,
            appearance=("memory-publication-width", "members-copy"))
        members = self.members | {audit}
        self.header_budget.debit(width(changed), appearance=("memory-publication-width", "delta-key-sort"))
        self.header_budget.debit(width(changed), appearance=("memory-publication-width", "delta-key-tuple"))
        keys = tuple(sorted(changed))
        self.header_budget.debit(len(keys) * (2 + sum(width(name) + 21 for name in (
            "table_id", "key", "before_digest", "after_digest")) + 3) + 2 + 21 * len(keys),
            appearance=("memory-publication-width", "delta-numeric-projection"))
        delta_width = array(tuple(obj({"table_id": width(table), "key": width(key),
            "before_digest": 66, "after_digest": 66}) for table, key in keys))
        accounting = await self._publication_accounting_payload(
            _numeric_body_certificate=certificate, _native_memory_operation=operation)
        self.header_budget.debit(2 + sum(width(key) + 21 for key in accounting)
            + max(0, len(accounting) - 1),
            appearance=("memory-publication-width", "checkpoint-numeric-map"))
        checkpoint_fields = {key: width(value) for key, value in accounting.items()}
        self.header_budget.debit(2 + sum(width(key) + 21 for key in (
            "schema_version", "composition_base", "composition_target", "composition_delta",
            "secret_values_included")) + 4,
            appearance=("memory-publication-width", "checkpoint-numeric-update"))
        checkpoint_fields.update(schema_version=1, composition_base=max(width(self.base), target_width),
            composition_target=target_width, composition_delta=delta_width, secret_values_included=5)
        checkpoint_width = obj(checkpoint_fields)
        prior = read_lifecycle_receipt(self.workspace, header_budget=self.header_budget)
        receipt_copy_upper = width(prior) if prior else 2 + width("secret_values_included") + 1 + 5
        self.header_budget.debit(receipt_copy_upper,
            appearance=("memory-publication-width", "receipt-numeric-map"))
        receipt = prior or {"secret_values_included": False}
        self.header_budget.debit(2 + sum(width(key) + 21 for key in receipt) + max(0, len(receipt) - 1),
            appearance=("memory-publication-width", "receipt-field-width-map"))
        receipt_fields = {key: width(value) for key, value in receipt.items()}
        receipt_fields["runtime_composition"] = target_width
        witness_width = width(accounting["witness"]) if accounting.get("witness") is not None else 0
        if witness_width:
            receipt_fields["inference_accounting"] = witness_width
        receipt_width = obj(receipt_fields)
        if checkpoint_width > MAX_ACCOUNTING_CHECKPOINT_BYTES or receipt_width > MAX_LIFECYCLE_RECEIPT_BYTES:
            raise HeaderBoundsError("composition_publication_size_exceeded")
        names = ("target_bytes", "native_bytes", "programme_bytes", "checkpoint_bytes", "receipt_bytes",
            "members_bytes", "delta_bytes", "delta_key_bytes", "changed_members_bytes", "changed_touched_bytes",
            "delta_sort_bytes", "witness_bytes", "receipt_default_bytes", "receipt_target_update_bytes",
            "receipt_witness_update_bytes", "added_members")
        self.header_budget.debit(2 + sum(width(name) + 21 for name in names) + len(names) - 1 + width((audit,)),
            appearance=("memory-publication-width", "numeric-output"))
        result = {"target_bytes": target_width, "native_bytes": native_width,
            "programme_bytes": width(self.base["programme"]), "checkpoint_bytes": checkpoint_width,
            "receipt_bytes": receipt_width, "members_bytes": array(width(item) for item in members),
            "delta_bytes": delta_width, "delta_key_bytes": width(keys), "delta_sort_bytes": width(keys),
            "changed_members_bytes": width((audit,)),
            "changed_touched_bytes": 2 + sum(width((table, key)) + 1 + 66 for table, key in keys) + max(0, len(keys) - 1),
            "witness_bytes": witness_width, "receipt_default_bytes": 0 if prior else width(receipt),
            "receipt_target_update_bytes": receipt_width + width("runtime_composition") + 1 + target_width,
            "receipt_witness_update_bytes": receipt_width + width("inference_accounting") + 1 + witness_width,
            "added_members": (audit,)}
        # A's final receipt is B's prior receipt. Both replacements reserve the
        # complete larger copy; a smaller actual appearance receives no refund.
        retained = MappingProxyType(result)
        self._native_memory_publication_numeric_facts = (operation, prepared, certificate, retention_widths, retained)
        # These are two ORIGINAL engine publications, not two synthetic
        # snapshots. Their body/accounting/certification vectors are composed
        # separately. Preserve each allocation and private read occurrence.
        # The helper's largest label has three ASCII fields, each <=31 bytes;
        # its amount is an original int64 numerical bound. Pay its list and
        # tuple before either helper constructs a vector.
        engine_vector_upper = 2 + 27 + 28 * (3 + 2 + 2 + 3 * (31 + 2) + 20)
        for copy in ("list", "tuple"):
            self.header_budget.debit(engine_vector_upper,
                appearance=("memory-publication-vector", "engine", copy))
        engine_entries = _native_memory_engine_publication_numeric_entries(operation, prepared,
            certificate=certificate, retention_widths=retention_widths,
            publication_widths=retained)
        prior_reads = getattr(self, "_native_memory_prior_publication_numeric", None)
        for copy in ("list", "tuple"):
            self.header_budget.debit(_native_memory_prior_publication_vector_upper(),
                appearance=("memory-publication-vector", "prior", copy))
        prior_entries = _native_memory_prior_publication_numeric_entries(operation, prepared,
            certificate=certificate, retention_widths=retention_widths,
            publication_widths=retained, prior_reads=prior_reads)
        from src.memory.composition_headers import _metadata_size
        # Each phase triple adds its literal phase to one paid source pair.
        # The two phases retain all 56 pairs, including every None raw read.
        pair_bytes = _metadata_size(engine_entries) + _metadata_size(prior_entries)
        vector_upper = 2 + 111 + 2 * pair_bytes + 56 * (width("capture") + width("completion") + 4)
        for copy in ("phase-triples", "tuple", "owner-lineage"):
            self.header_budget.debit(vector_upper,
                appearance=("memory-publication-vector", "combined", copy))
        entries = tuple((phase, appearance, upper)
            for phase in ("capture", "completion")
            for vector in (engine_entries, prior_entries)
            for appearance, upper in vector)
        self._native_memory_publication_allocation_numeric = (
            operation, prepared, certificate, retention_widths, retained, entries)
        return retained

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
        from src.memory.header_bounds import (_MEMORY_LEDGER_PHASE,
            _NativeMemoryNumericLedger, _MEMORY_LEDGER_SEAL)
        active_memory = _MEMORY_LEDGER_PHASE.get()
        if (type(active_memory) is tuple and len(active_memory) == 2
                and type(active_memory[0]) is _NativeMemoryNumericLedger):
            ledger = active_memory[0]
            retention = getattr(self, "_native_memory_source_retention", None)
            if (ledger._complete_owner is not _MEMORY_LEDGER_SEAL
                    or type(retention) is not _NativeMemoryRetention
                    or retention.ledger is not ledger or retention.source is not ledger.source
                    or retention.operation is not ledger.operation
                    or ledger.budget is not self.header_budget):
                raise HeaderBoundsError("memory_retention_writer_changed")
            # The original publisher selects the same already-issued numeric
            # payer. Current Source/task/transaction/driver/guard and any
            # completion publication proof are checked by the existing owner;
            # this supplies neither a certificate nor body permission.
            from src.memory.composition_headers import _metadata_memory_capacity
            current_connection = connection if connection is not None else self.db.sync_session.connection()
            _metadata_memory_capacity(current_connection, self.header_budget, active_memory)
            return self.header_budget
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
        plan = _ARMED_MEMORY_PUBLICATION.get()
        if type(plan) is _NativeMemorySourcePublication:
            import asyncio
            from src.memory.composition_headers import current_budget, _CURRENT
            from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, _native_memory_utf8_width
            if (_MEMORY_PUBLICATIONS.get(id(plan)) is not plan
                    or plan.seal is not _MEMORY_PUBLICATION_SEAL or plan.db is not self.db
                    or plan.statement is not statement or plan.task is not asyncio.current_task()):
                raise ProductionWorkspaceReconciliationError("memory_private_journal_owner_changed")
            retention = plan.retention._checked()
            active = _MEMORY_LEDGER_PHASE.get()
            if (_MEMORY_PUBLICATIONS.get(id(plan)) is not plan or plan.seal is not _MEMORY_PUBLICATION_SEAL
                    or plan.consumed or plan.applied or plan.db is not self.db
                    or plan.statement is not statement or plan.task is not asyncio.current_task()
                    or self.db.sync_session.get_transaction() is not plan.transaction
                    or run_id != plan.before['run_identity']
                    or before != plan.before['checkpoint_receipts_json']
                    or after != plan.after['checkpoint_receipts_json']
                    or current_budget() is not self.header_budget
                    or _CURRENT.get() is not plan.certificate
                    or plan.certificate.connection.connection.driver_connection is not plan.driver
                    or retention.source.admission.header_budget is not self.header_budget
                    or active is None or active[0] is not retention.ledger
                    or active[1] != ('completion' if plan.kind == 'completion' else 'capture')):
                raise ProductionWorkspaceReconciliationError("memory_private_journal_owner_changed")
            facts = retention._publication_numeric_facts
            numeric = _native_memory_private_journal_numeric_entries(retention.operation,
                retention.prepared, certificate=facts[2], retention_widths=plan._numeric_widths,
                kind=plan.kind, affected_proof=plan.affected_proof)
            if max(6 * _native_memory_utf8_width(before or '[]') + 2,
                    6 * _native_memory_utf8_width(after or '[]') + 2) > plan._numeric_widths['journal_upper']:
                raise ProductionWorkspaceReconciliationError("memory_private_journal_bound_changed")
            for appearance, upper in numeric:
                if appearance[-1] not in {'source-branch-parse', 'numeric-amounts-tuple', 'numeric-vector'}:
                    self.header_budget.debit(upper, appearance=appearance)
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
        if any(identifier.startswith("runtime-service-invocation:programme-start:")
                for identifier in current.keys() - previous.keys()):
            from src.runtime_plugins.programme_producer import validate_programme_start_publication
            programme_permission = validate_programme_start_publication(
                self.db, statement, run_id, before, after, previous, current)
            permitted = (*permitted, *programme_permission)
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
                                              discovery_session=None, _native_memory_operation=None):
        """Original unchanged ledger/readback predicates, in this same session."""
        if _numeric_body_certificate is not None:
            from src.memory.composition_headers import _validate
            from src.memory.header_bounds import (_MEMORY_LEDGER_PHASE,
                _NativeMemoryNumericLedger, _NativeProgrammeNumericLedger)
            active_native = _MEMORY_LEDGER_PHASE.get()
            native_owner = False
            if _native_memory_operation is not None:
                from src.runtime_plugins.memory_producer import _validate_memory_owner_source
                source = await _validate_memory_owner_source(self.db,
                    _native_memory_operation.run, _native_memory_operation)
                projection = getattr(self, "_native_memory_source_projection", None)
                if (projection is None or projection[0] is not _native_memory_operation
                        or _native_memory_operation._effect is not None
                        or _native_memory_operation._prepared.consumed
                        or source.admission.header_budget is not self.header_budget
                        or self.db.info.get("composition_guard") is not self):
                    raise ProductionWorkspaceReconciliationError("memory_numeric_publication_unavailable")
                native_owner = True
            if active_native is not None:
                ledger, phase = active_native
                ledger._checked()
                retention = getattr(self, "_native_memory_source_retention", None)
                programme = getattr(self, "_native_programme_start_numeric_owner", None)
                if type(ledger) is _NativeMemoryNumericLedger:
                    native_owner = (type(retention) is _NativeMemoryRetention
                        and retention.ledger is ledger and retention.source is ledger.source
                        and phase in {"capture", "completion"})
                elif type(ledger) is _NativeProgrammeNumericLedger:
                    native_owner = (type(programme) is tuple and len(programme) == 3
                        and programme[0] is ledger.source and phase == "publication")
                if (not native_owner or ledger.budget is not self.header_budget
                        or ledger.bindings.get(phase, (None,))[0] is not self.db
                        or self.db.info.get("composition_guard") is not self):
                    raise ProductionWorkspaceReconciliationError("composition_numeric_publication_unavailable")
            prospective = self._prospective_admission
            if discovery_session is not None:
                from src.guardian.goal_discovery import _OwnedDiscoverySession
                if type(discovery_session) is not _OwnedDiscoverySession:
                    raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
                discovery_session.validate(db=self.db, header_budget=self.header_budget)
                if discovery_session.guard is not self or discovery_session._candidate is None:
                    raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
            elif not native_owner and (prospective is None or _numeric_body_certificate is not prospective[0].common33
                    or _numeric_body_certificate.budget is not self.header_budget
                    or self.db.info.get("composition_guard") is not self):
                raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
            connection = await self.db.connection()
            def validate_numeric_reader(conn):
                if discovery_session is None and not native_owner:
                    self._validate_programme_selection(prospective[0])
                elif (_numeric_body_certificate.connection is not conn
                        or _numeric_body_certificate.budget is not self.header_budget
                        or _numeric_body_certificate.raw_owner is not None):
                    raise ProductionWorkspaceReconciliationError("programme_numeric_publication_unavailable")
                _validate(conn, _numeric_body_certificate)
            await connection.run_sync(validate_numeric_reader)
        accounting = self.db.info.get("composition_accounting_payload")
        accounting_numeric_observed = False
        if accounting is None:
            accounting_numeric_observed = True
            # A composition-only writer must retain the unchanged accounting
            # owner proof. Dropping it would regress the shared checkpoint.
            from sqlalchemy import select
            from src.db.models import InferenceAccountingOwner, InferenceCostReservation
            from src.workflows.inference_accounting import _witness, _ledger_digest
            from src.workspace.production import read_accounting_checkpoint
            if _native_memory_operation is None:
                prior = read_accounting_checkpoint(self.workspace, header_budget=self.header_budget)
                lifecycle = read_lifecycle_receipt(self.workspace, header_budget=self.header_budget) or {}
            else:
                # Observe only the two original reads already owned here.
                # Pair/list storage is prepaid before entering the tracer;
                # the original private bytes never enter the retained facts.
                import asyncio
                from src.memory.header_bounds import _trace_memory_numeric_charges
                from src.memory.composition_headers import _metadata_size
                def read_prior(reader, label):
                    stages = (("json-parse", "materialization", "return") if label == "checkpoint"
                        else ("decode", "json-parse", "materialization", "return"))
                    pair_upper = 2 + 4 + 1 + 20
                    storage_upper = 2 + 2 * pair_upper + 1
                    for stage in stages:
                        pair_upper = 2 + _metadata_size(("composition-publication", label, stage)) + 1 + 20
                        storage_upper += 2 * pair_upper + 1
                    self.header_budget.debit(storage_upper,
                        appearance=("memory-publication-prior-trace", label, "pair-list-storage"))
                    with _trace_memory_numeric_charges(self.header_budget) as trace:
                        value = reader(self.workspace, header_budget=self.header_budget)
                    self.header_budget.debit(_metadata_size(trace),
                        appearance=("memory-publication-prior-trace", label, "retained-tuple"))
                    from src.workspace.production import _publication_json_upper
                    self.header_budget.debit(3 + _publication_json_upper(value) + _metadata_size(trace),
                        appearance=("memory-publication-prior-trace", label, "reader-return"))
                    return value, tuple(trace)
                prior, checkpoint_trace = read_prior(read_accounting_checkpoint, "checkpoint")
                lifecycle, receipt_trace = read_prior(read_lifecycle_receipt, "receipt")
                lifecycle = lifecycle or {}
                self.header_budget.debit(2 + 6 + 5 * 4
                    + _metadata_size(checkpoint_trace) + _metadata_size(receipt_trace),
                    appearance=("memory-publication-prior-trace", "owner-lineage"))
                self._native_memory_prior_publication_numeric = (
                    _native_memory_operation, _native_memory_operation._prepared,
                    _numeric_body_certificate, self.header_budget, asyncio.current_task(),
                    checkpoint_trace, receipt_trace)
            if self.header_budget is not None:
                from src.memory.header_bounds import COMPOSITION_DESCRIPTORS
                from src.memory.composition_headers import locate_exact_rows
                payer = self.header_budget if _numeric_body_certificate is not None else None
                if _native_memory_operation is None:
                    ids = await locate_exact_rows(self.db, COMPOSITION_DESCRIPTORS["inference_accounting_owners"], "deployment",
                        self.header_budget, _header_budget=payer)
                    await self.header_budget.certify(self.db, COMPOSITION_DESCRIPTORS["inference_accounting_owners"], ids,
                        _header_budget=payer)
                    await self.header_budget.certify_all(self.db, COMPOSITION_DESCRIPTORS["inference_cost_reservations"],
                        _header_budget=payer)
                else:
                    self.header_budget.debit(2 + 6 * (6 * 32 + 2 + 4 + 2) + 16 + 2,
                        appearance=("native-memory-accounting-certification-trace", "context-wrapper"))
                    async with _native_memory_accounting_certification_capture(
                            self, _native_memory_operation, _numeric_body_certificate):
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
                self.header_budget.debit(2 + sum(upper + 1
                    for (table, _key), (_row, upper) in _numeric_body_certificate.rows.items()
                    if table == "inference_cost_reservations"),
                    appearance=("publication-accounting", "reservation-list"))
            rows = list((await self.db.execute(select(InferenceCostReservation))).scalars())
            if _native_memory_operation is not None:
                # These are the SAME original ORM bodies read above. Retain
                # only their source-owned witness/ledger numeric schedule for
                # the later two engine publications; no model/body survives
                # as permission and no additional query is performed.
                from src.workflows.inference_accounting import (
                    original_accounting_witness_numeric_schedule, _accounting_scalar_upper)
                from src.memory.composition_headers import _metadata_size
                import asyncio
                count = 17
                pair_sum = 17 * (4 + 20 + 2 + 3 + 4 * 128)
                if account is not None:
                    # At most fourteen operation appearances: two dumps and
                    # three for each of four nullable datetime fields. Fixed
                    # label strings are <=21 ASCII bytes; IDs are original
                    # paid typed model strings, measured without serialization.
                    pair_sum += 17 * _accounting_scalar_upper(account.deployment_id)
                    for row in rows:
                        count += 14
                        pair_sum += 14 * (4 + 20 + 2 + 3 + 4 * 128
                            + _accounting_scalar_upper(row.operation_id))
                if account is None:
                    numeric = ()  # original absent-owner branch constructs no vector
                else:
                    vector_upper = 2 + max(0, count - 1) + pair_sum
                    self.header_budget.debit(6 * vector_upper,
                        appearance=("memory-publication-accounting", "planning-structures"))
                    numeric = original_accounting_witness_numeric_schedule(account, rows)
                self.header_budget.debit(2 + 6 + 5 * 20 + _metadata_size(numeric),
                    appearance=("memory-publication-accounting", "owner-lineage"))
                self._native_memory_accounting_numeric = (
                    _native_memory_operation, _native_memory_operation._prepared,
                    _numeric_body_certificate, self.header_budget, asyncio.current_task(),
                    numeric)
            if account is None:
                if rows or lifecycle.get("inference_accounting") is not None or (prior and prior.get("witness") is not None):
                    raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
                if _numeric_body_certificate is not None:
                    self.header_budget.debit(2,
                        appearance=("publication-accounting", "empty-payload"))
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
                if _numeric_body_certificate is not None:
                    from src.workspace.production import _publication_json_upper
                    # The five original fields have fixed ASCII keys. Size
                    # the original payload directly, avoiding a temporary
                    # numeric sizing map before its allocation payment.
                    payload_upper = (2 + 4 + len("base") + 3
                        + _publication_json_upper(prior.get("base"))
                        + len("account") + 3 + _publication_json_upper(prior["account"])
                        + len("operations") + 3 + 2
                        + len("witness") + 3 + _publication_json_upper(witness)
                        + len("secret_values_included") + 3 + 5)
                    self.header_budget.debit(payload_upper,
                        appearance=("publication-accounting", "payload-construction"))
                accounting = {"base": prior.get("base"), "account": prior["account"],
                    "operations": [], "witness": witness, "secret_values_included": False}
        if _native_memory_operation is not None and accounting_numeric_observed:
            # Original prior/account/witness values produced this exact
            # payload width. Retain only its numeric liability and branch;
            # neither the private values nor a future payload are retained.
            self.header_budget.debit(2 + 6 + 5 * 4 + 5 + 20,
                appearance=("memory-publication-accounting", "payload-width-lineage"))
            self._native_memory_accounting_payload_numeric = (
                _native_memory_operation, _native_memory_operation._prepared,
                _numeric_body_certificate, self.header_budget, asyncio.current_task(),
                account is None, 2 if account is None else payload_upper)
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
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
        native_exit = _MEMORY_LEDGER_PHASE.get()
        accounting = await self._publication_accounting_payload(
            _numeric_body_certificate=self._publication_common if owned_exit is not None or native_exit is not None else None,
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
        """The original engine exit consumes the actual native owner's frame."""
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE, _NativeMemoryNumericLedger
        retention = getattr(self, "_native_memory_source_retention", None)
        programme_owner = getattr(self, "_native_programme_start_numeric_owner", None)
        if retention is not None and programme_owner is not None:
            raise ProductionWorkspaceReconciliationError("composition_numeric_owner_ambiguous")
        if retention is not None:
            if (type(retention) is not _NativeMemoryRetention
                    or _MEMORY_RETENTIONS.get(id(retention.source)) is not retention
                    or retention.source._retention is not retention
                    or type(retention.ledger) is not _NativeMemoryNumericLedger
                    or self.db.info.get("composition_guard") is not self
                    or self.header_budget is not retention.ledger.budget
                    or not retention.captured or _MEMORY_LEDGER_PHASE.get() is not None):
                raise ProductionWorkspaceReconciliationError("memory_publication_source_changed")
            retention.ledger._checked()
            phase = "capture" if self.db is retention.operation.db else "completion"
            if phase == "completion" and not retention.completed:
                raise ProductionWorkspaceReconciliationError("memory_publication_completion_missing")
            if phase == "completion":
                import asyncio
                plan = getattr(retention, "_completion_publication", None)
                binding = retention.ledger.bindings.get("completion")
                if (type(plan) is not _NativeMemorySourcePublication
                        or _MEMORY_PUBLICATIONS.get(id(plan)) is not plan
                        or plan.retention is not retention or plan.kind != "completion"
                        or not plan.consumed or not plan.applied
                        or plan.db is not self.db or plan.task is not asyncio.current_task()
                        or plan.transaction is not self.db.sync_session.get_transaction()
                        or not plan.driver.in_transaction or binding is None
                        or binding[0] is not self.db or binding[1] is not plan.transaction
                        or binding[2] is not plan.driver or binding[5] is not plan.task
                        or getattr(plan, "_capacity_publication_live", False)
                        or getattr(plan, "_capacity_publication_spent", False)):
                    if type(plan) is _NativeMemorySourcePublication and plan.retention is retention:
                        plan._capacity_publication_spent = True
                        if _MEMORY_PUBLICATIONS.get(id(plan)) is plan:
                            _MEMORY_PUBLICATIONS.pop(id(plan))
                    retention._completion_publication = None
                    raise ProductionWorkspaceReconciliationError("memory_publication_completion_proof_changed")
                # This grants numerical payment only for the original writer's
                # engine exit. Completed Sources cannot acquire another writer.
                plan._capacity_publication_live = True
                try:
                    with retention.ledger.phase("completion"):
                        await self._publish_current_writer()
                finally:
                    plan._capacity_publication_live = False
                    plan._capacity_publication_spent = True
                    if _MEMORY_PUBLICATIONS.get(id(plan)) is plan:
                        _MEMORY_PUBLICATIONS.pop(id(plan))
                    retention._completion_publication = None
            else:
                await retention.ledger.bind(self.db, phase)
                with retention.ledger.phase(phase):
                    await self._publish_current_writer()
            return
        if programme_owner is not None:
            from src.runtime_plugins.programme_producer import _ProgrammeSource, _StartPublication
            from src.memory.header_bounds import _NativeProgrammeNumericLedger
            source, called, run = programme_owner
            proof = self.db.info.get("composition_native_programme_start")
            ledger = getattr(source, "_numeric_reservation", None)
            if (type(source) is not _ProgrammeSource or type(proof) is not _StartPublication
                    or type(ledger) is not _NativeProgrammeNumericLedger
                    or source.publication is not proof or proof.source is not source
                    or proof.called is not called or proof.db is not self.db
                    or run.run_identity != source.job_id or not proof.checked or proof.committed
                    or self.header_budget is not ledger.budget
                    or self.db.info.get("composition_guard") is not self
                    or _MEMORY_LEDGER_PHASE.get() is not None):
                raise ProductionWorkspaceReconciliationError("programme_publication_source_changed")
            await ledger.bind(self.db, "publication")
            with ledger.phase("publication"):
                await self._publish_current_writer()
            return
        await self._publish_current_writer()

    async def _publish_current_writer(self):
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
                from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
                if discovery_reservation is None and _MEMORY_LEDGER_PHASE.get() is None:
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
        retention = getattr(self, "_native_memory_source_retention", None)
        if type(retention) is _NativeMemoryRetention:
            plan = getattr(retention, "_completion_publication", None)
            if (type(plan) is _NativeMemorySourcePublication
                    and plan.retention is retention and plan.db is self.db
                    and plan.kind == "completion"):
                plan._capacity_publication_live = False
                plan._capacity_publication_spent = True
                if _MEMORY_PUBLICATIONS.get(id(plan)) is plan:
                    _MEMORY_PUBLICATIONS.pop(id(plan))
                retention._completion_publication = None
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


async def prepare_composition_session(db, *, fresh=False, header_budget=None, _native_memory_source=None):
    """Acquire external ownership BEFORE any native SQL writer starts."""
    from sqlalchemy import text
    from config.settings import settings
    memory_retention = None
    if _native_memory_source is not None:
        import asyncio
        from src.runtime_plugins.memory_producer import (
            _NativeMemoryDispatchSource, _validate_memory_completion_capacity_source)
        from src.memory.header_bounds import _MEMORY_LEDGER_PHASE
        if type(_native_memory_source) is not _NativeMemoryDispatchSource:
            raise ProductionWorkspaceReconciliationError("memory_retention_source_unavailable")
        source = _validate_memory_completion_capacity_source(
            _native_memory_source, _native_memory_source.claim)
        memory_retention = source._retention
        if (header_budget is not memory_retention.ledger.budget
                or _MEMORY_LEDGER_PHASE.get() != (memory_retention.ledger, "completion")
                or memory_retention.ledger._completion_capacity_task is not asyncio.current_task()):
            raise ProductionWorkspaceReconciliationError("memory_retention_source_changed")
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
        memory_guard = None
        if type(expected) is dict and expected.get("schema_version") == 2:
            if header_budget is None:
                raise ProductionWorkspaceReconciliationError("programme_original_budget_required")
            _validate_programme_envelope(expected)
            programme_guard = CompositionSessionGuard(db, workspace, lock, expected, set(), header_budget=header_budget)
            db.info["composition_guard"] = programme_guard
        connection = await db.connection()
        if memory_retention is not None:
            # Retain the same original guard that this preparation installs
            # below; moving its creation precedes numerical binding, not reads.
            memory_guard = programme_guard or CompositionSessionGuard(
                db, workspace, lock, expected, set(), header_budget=header_budget)
            db.info["composition_guard"] = memory_guard
            memory_guard._native_memory_source_retention = memory_retention
            raw = await connection.get_raw_connection()
            if not raw.driver_connection.in_transaction:
                await connection.exec_driver_sql("BEGIN")
            await memory_retention.ledger.bind(db, "completion")
        def checked_snapshot(conn):
            if header_budget is None:
                return composition_closure(conn, verify_files=native_composition_files)
            from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection, snapshot_reads
            if not conn.connection.driver_connection.in_transaction:
                conn.exec_driver_sql("BEGIN")
            certificate = _certify_current_memory_snapshot_on_connection(
                conn, header_budget, _header_budget=header_budget)
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
        guard = programme_guard or memory_guard or CompositionSessionGuard(db, workspace, lock, base, members, header_budget=header_budget)
        guard.base, guard.members = base, members
        if programme_guard is not None:
            guard._programme_base_leaves = dict(guard._programme_current_leaves)
        db.info["composition_guard"] = guard
        db.info["composition_base_witness"] = base
        guard.install()
        return guard
    except BaseException:
        prepared_guard = locals().get("programme_guard") or locals().get("memory_guard")
        if prepared_guard is not None and db.info.get("composition_guard") is prepared_guard:
            db.info.pop("composition_guard", None)
            _HELD_COMPOSITION_WORKSPACE.reset(prepared_guard.token)
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
    lock = maintenance_accounting_lock(Path(settings.workspace_dir).resolve(), header_budget=header_budget)
    workspace = lock.__enter__()
    guard = None
    try:
        receipt = read_lifecycle_receipt(workspace, header_budget=header_budget) or {}
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
