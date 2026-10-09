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


def validate_retained_table_schema(connection, table, *, error="composition_projection_schema_changed"):
    """Only the reviewed NULL-only Session addition is DDL-compatible with v3."""
    columns = list(_sql(connection, f'PRAGMA table_info("{table}")'))
    actual = {row[1] for row in columns}
    expected = set(RETAINED_FIELDS[table])
    if table != "sessions":
        if actual != expected:
            raise ProductionWorkspaceReconciliationError(error)
        return
    index_name = "ix_sessions_continuity_task_id"
    named = list(_sql(connection, "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?", (index_name,)))
    if actual == expected:
        if named:
            raise ProductionWorkspaceReconciliationError(error)
        return
    if actual != expected | {"continuity_task_id"}:
        raise ProductionWorkspaceReconciliationError(error)
    column = next(row for row in columns if row[1] == "continuity_task_id")
    if tuple(column[1:]) != tuple(NULL_SESSION_DDL_DESCRIPTOR["column"]):
        raise ProductionWorkspaceReconciliationError(error)
    all_foreign = [tuple(row) for row in _sql(connection, 'PRAGMA foreign_key_list("sessions")')]
    foreign = [row for row in all_foreign if row[3] == "continuity_task_id"]
    if (len(foreign) != 1 or foreign[0][1] != 0
        or foreign[0][2:] != tuple(NULL_SESSION_DDL_DESCRIPTOR["foreign_key"])
        or sum(row[0] == foreign[0][0] for row in all_foreign) != 1):
        raise ProductionWorkspaceReconciliationError(error)
    if len(named) != 1 or named[0][0] != "sessions":
        raise ProductionWorkspaceReconciliationError(error)
    indexes = [row for row in _sql(connection, 'PRAGMA index_list("sessions")') if row[1] == index_name]
    if len(indexes) != 1 or tuple(indexes[0][2:]) != (0, "c", 0):
        raise ProductionWorkspaceReconciliationError(error)
    info = [tuple(row) for row in _sql(connection, f'PRAGMA index_info("{index_name}")')]
    keys = [tuple(row) for row in _sql(connection, f'PRAGMA index_xinfo("{index_name}")') if row[5] == 1]
    if info != [(0, column[0], "continuity_task_id")] or keys != [(0, column[0], "continuity_task_id", 0, "BINARY", 1)]:
        raise ProductionWorkspaceReconciliationError(error)


def _composition_row(connection, table, key):
    if table not in RETAINED_FIELDS or type(key) is not str or len(key.encode()) > 512:
        raise ProductionWorkspaceReconciliationError("composition_reference_invalid")
    columns = RETAINED_FIELDS[table]
    names = ",".join('"' + field + '"' for field in columns)
    result = _sql(connection, f'SELECT {names} FROM "{table}" WHERE "{COMPOSITION_KEYS[table]}"=?', (key,)).fetchall()
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


def composition_closure(connection, *, verify_files=None):
    """Finite native joins. Stream row bodies; publish only hashes and counts.

    ``verify_files`` is the reviewed private native reader, never child input.
    Missing extension readers block rather than inventing filesystem proof.
    """
    from src.runtime_plugins.ownership import DOMAINS, RuntimeCompositionBinding, CompositionDependency
    tables = {row[0] for row in _sql(connection, "SELECT name FROM sqlite_master WHERE type='table'")}
    if "runtime_composition_states" not in tables:
        return None, set()
    inventory = list(_sql(connection, "SELECT runtime_domain,owner_kind,epoch,composition_digest,state,recovery_receipt_ref FROM runtime_composition_states ORDER BY runtime_domain"))
    if not inventory:
        return None, set()
    if len(inventory) != 14 or {row[0] for row in inventory} != set(DOMAINS):
        raise ProductionWorkspaceReconciliationError("composition_inventory_incomplete")
    for row in inventory:
        CompositionDependency(*row[:4])
        if row[4] not in {"ready", "draining", "blocked"}:
            raise ProductionWorkspaceReconciliationError("composition_inventory_invalid")
    for table, fields in RETAINED_FIELDS.items():
        if table not in tables:
            raise ProductionWorkspaceReconciliationError("composition_projection_schema_changed")
        validate_retained_table_schema(connection, table)
    pending = [("runtime_composition_states", row[0]) for row in inventory]
    pending.extend(("workflow_run_states", row[0]) for row in _sql(connection,
        "SELECT run_identity FROM workflow_run_states WHERE composition_binding_json IS NOT NULL"))
    if "inference_cost_reservations" in tables:
        pending.extend(("workflow_run_states", row[0]) for row in _sql(connection,
            "SELECT DISTINCT job_id FROM inference_cost_reservations"))
    visited = set()
    artifacts = {}
    roles = set()
    audit_predecessors = {}
    native_read_audits = {}
    def add(table, field, value):
        if value is not None:
            for row in _sql(connection, f'SELECT "{COMPOSITION_KEYS[table]}" FROM "{table}" WHERE "{field}"=?', (value,)):
                pending.append((table, row[0]))
    def required(table, value):
        if value is not None:
            pending.append((table, value))
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
            session_role("legacy_job_session_fk", table, key, row["session_id"])
            binding = row["composition_binding_json"]
            if binding is not None:
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
                elif row["job_kind"] not in {"workflow", "conversation_turn_v1", "research_dossier", "readonly_research_child", "runtime_service_read_v1"}:
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
            if (binding is not None or outputs is not None) and (json.loads(row["artifact_receipts_json"]) or json.loads(row["checkpoint_receipts_json"])):
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
    digest = hashlib.sha256(b"seraph-continuity-closure-v1\0")
    counts = {}
    for table, key in sorted(visited, key=lambda item: (item[0], item[1].encode())):
        leaf = composition_row_digest(table, key, _composition_row(connection, table, key))
        for value in (table, key, leaf):
            encoded = value.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        counts[table] = counts.get(table, 0) + 1
    inventory_payload = [dict(zip(COMPOSITION_FIELDS["runtime_composition_states"], row)) for row in inventory]
    role_counts = {}
    for role, *_ in roles:
        role_counts[role] = role_counts.get(role, 0) + 1
    return {"schema_version": 1, "projection_schema": COMPOSITION_SCHEMA,
        "extension_version": NATIVE_EXTENSION_VERSION, "extension_digest": NATIVE_EXTENSION_DIGEST,
        "inventory": inventory_payload,
        "inventory_digest": hashlib.sha256(json.dumps(inventory_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "closure_digest": digest.hexdigest(), "total_rows": len(visited), "table_counts": counts,
        "relation_counts": role_counts, "relation_digest": hashlib.sha256(json.dumps(sorted(roles), separators=(",", ":")).encode()).hexdigest(),
        "artifact_count": len(artifacts), "artifact_manifest_digest": hashlib.sha256(json.dumps(
            sorted(artifacts.values()), separators=(",", ":")).encode()).hexdigest()}, visited


def validate_composition_progression(previous, incoming):
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


def native_composition_files(table, row, *, root=None):
    """Positive bytes through the existing fixed private artifact reader."""
    from types import SimpleNamespace
    from config.settings import settings
    from src.workspace import canonical_workspace_root
    from src.work_board.input_artifacts import _payload_path, _safe_file_bytes
    if table == "work_board_input_artifacts":
        path = _payload_path(SimpleNamespace(**row))
        if root is not None:
            path = Path(root) / row["typed_input_ref"][len("workspace-json:"):]
        _safe_file_bytes(path, expected_digest=row["payload_sha256"], expected_size=row["size_bytes"])
        yield row["typed_input_ref"], row["payload_sha256"], row["size_bytes"], "private_input"
        return
    if table != "workflow_run_states":
        raise ProductionWorkspaceReconciliationError("composition_private_reader_unsupported")
    from src.runtime_plugins.inference_output import checked_output_records, OUTPUT_ID, read_output_bytes, verify_output_envelope
    outputs = checked_output_records(row)
    if outputs is not None and outputs[-1]["checkpoint_id"] == OUTPUT_ID:
        payload = outputs[-1]["payload"]
        workspace = Path(root) if root is not None else canonical_workspace_root(settings.workspace_dir)
        verify_output_envelope(read_output_bytes(workspace, payload), payload)
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
                         expected_digest=digest, expected_size=size)
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


async def prepare_composition_read_session(db):
    """Cheap inventory-presence check; never private files or publication."""
    from sqlalchemy import text
    exists = await db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_composition_states'"))
    populated = await db.scalar(text("SELECT 1 FROM runtime_composition_states LIMIT 1")) if exists else None
    await db.rollback()
    if not populated:
        return None
    guard = CompositionReadGuard(db)
    guard.install()
    db.info["composition_read_guard"] = guard
    return guard


class CompositionSessionGuard:
    """One existing session, external lock and precommit publisher.

    Native owners opt into their complete atomic writer. Unmarked writes to
    retained leaves (including bulk SQL) fail before mutation/publication.
    """
    def __init__(self, db, workspace, lock, witness_value, members):
        self.db, self.workspace, self.lock = db, workspace, lock
        self.base, self.members = witness_value, members
        self.touched = {}
        self.token = _HELD_COMPOSITION_WORKSPACE.set(workspace)
        self.listeners = []
        self.connection_listeners = []
        self.prepared_commit = False
        self._legacy_continuity_metadata = {}

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
        if (table, key) in self.touched:
            return
        if len(self.touched) >= 128:
            raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
        owner = self.db.info.get("composition_writer_owner")
        if owner not in {"durable_jobs", "native_ingress", "composition_maintenance", "finite_service"}:
            raise ProductionWorkspaceReconciliationError("composition_unhooked_writer")
        before = None if creating else composition_row_digest(table, key, _composition_row(connection, table, key))
        self.touched[(table, key)] = before

    def _check_private_journal(self, before, after, *, run_id):
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
        task_ids = {"runtime-service-invocation:task-capability:" + phase
            for phase in ("admission", "source", "invoke", "outcome", "cleanup")}
        if task_ids.intersection(current):
            from src.runtime_plugins.task_capability import preflight_report_journal
            preflight_report_journal(json.loads(after or "[]"))
        family_permission = self.db.info.get("composition_native_turn_family_receipt")
        permitted = (self.db.info.get("composition_native_claim_receipt"), self.db.info.get("composition_native_turn_receipt"), family_permission)
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
                source = state.statement.text.lower().lstrip()
                if not source.startswith(("select", "begin", "pragma")) and any(table in source for table in RETAINED_FIELDS):
                    raise ProductionWorkspaceReconciliationError("composition_unhooked_bulk_sql")
            if not (state.is_update or state.is_delete):
                return
            statement = state.statement
            table = getattr(statement, "table", None)
            name = getattr(table, "name", None)
            if name not in RETAINED_FIELDS:
                return
            values = getattr(statement, "_values", {}) or {}
            if name == "workflow_run_states" and any(
                getattr(column, "name", column) in {"composition_binding_json", "source_task_id"} for column in values
            ):
                raise ProductionWorkspaceReconciliationError("composition_binding_retrofit_denied")
            key = COMPOSITION_KEYS[name]
            lookup = select(table.c[key])
            if statement.whereclause is not None:
                lookup = lookup.where(statement.whereclause)
            records = state.session.execute(lookup).scalars()
            connection = state.session.connection()
            for identity in records:
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
                            self._check_private_journal(_composition_row(connection, name, identity)["checkpoint_receipts_json"], raw, run_id=identity)
                if (name, identity) in self.members:
                    self._touch(connection, name, identity)
            state.update_execution_options(_composition_tracked_writer=self)
        def connection_started(session, transaction, connection):
            def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                compiled = getattr(context, "compiled", None)
                operation = getattr(compiled, "statement", None)
                table = getattr(getattr(operation, "table", None), "name", None)
                dml = any(getattr(operation, kind, False) for kind in ("is_insert", "is_update", "is_delete"))
                if compiled is None or isinstance(operation, TextClause):
                    source = statement.lower().lstrip()
                    dml = not source.startswith(("select", "begin", "pragma", "savepoint", "release", "rollback"))
                    protected = dml and any(name in source for name in RETAINED_FIELDS)
                else:
                    protected = dml and table in RETAINED_FIELDS
                if protected and not (session._flushing or context.execution_options.get("_composition_tracked_writer") is self):
                    raise ProductionWorkspaceReconciliationError("composition_unhooked_connection_sql")
            guarded = poison(before_cursor_execute)
            event.listen(connection, "before_cursor_execute", guarded)
            self.connection_listeners.append((connection, guarded))
        def before_flush(session, context, instances):
            connection = session.connection()
            for value in (*session.new, *session.dirty, *session.deleted):
                table = getattr(value, "__tablename__", None)
                if table not in RETAINED_FIELDS:
                    continue
                key = getattr(value, COMPOSITION_KEYS[table])
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
                self.prepared_commit = False
                return
            session.flush()
            if self.touched or session.info.get("composition_accounting_payload"):
                raise ProductionWorkspaceReconciliationError("composition_unpublished_internal_commit")
        for name, callback in (("do_orm_execute", before_execute), ("before_flush", before_flush),
                               ("after_begin", connection_started), ("before_commit", before_commit)):
            guarded = poison(callback)
            event.listen(self.db.sync_session, name, guarded)
            self.listeners.append((name, guarded))

    async def publish(self):
        from src.workspace.production import write_accounting_checkpoint, write_lifecycle_receipt
        if self.db.info.get("composition_sticky_failure") is not None:
            raise self.db.info["composition_sticky_failure"]
        await self.db.flush()
        if self.db.info.get("composition_sticky_failure") is not None:
            raise self.db.info["composition_sticky_failure"]
        connection = await self.db.connection()
        target, members = await connection.run_sync(lambda conn: composition_closure(conn, verify_files=native_composition_files))
        changed_members = members.symmetric_difference(self.members)
        if changed_members and self.db.info.get("composition_writer_owner") not in {
                "durable_jobs", "native_ingress", "composition_maintenance", "finite_service"}:
            raise ProductionWorkspaceReconciliationError("composition_unhooked_membership_writer")
        if len(set(self.touched) | changed_members) > 128:
            raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
        for table, key in changed_members:
            if (table, key) not in self.touched:
                # Newly retained ancestors must be included in the shared delta.
                self.touched[(table, key)] = None if (table, key) not in self.members else await connection.run_sync(
                    lambda conn, table=table, key=key: composition_row_digest(table, key, _composition_row(conn, table, key)))
        if not self.touched and target != self.base:
            raise ProductionWorkspaceReconciliationError("composition_unhooked_membership_writer")
        delta = []
        for (table, key), before in sorted(self.touched.items()):
            after = await connection.run_sync(lambda conn, table=table, key=key:
                composition_row_digest(table, key, _composition_row(conn, table, key))) if (table, key) in members else None
            delta.append({"table_id": table, "key": key, "before_digest": before, "after_digest": after})
        accounting = self.db.info.get("composition_accounting_payload")
        if accounting is None:
            # A composition-only writer must retain the unchanged accounting
            # owner proof. Dropping it would regress the shared checkpoint.
            from sqlalchemy import select
            from src.db.models import InferenceAccountingOwner, InferenceCostReservation
            from src.workflows.inference_accounting import _witness, _ledger_digest
            from src.workspace.production import read_accounting_checkpoint
            prior = read_accounting_checkpoint(self.workspace)
            lifecycle = read_lifecycle_receipt(self.workspace) or {}
            account = await self.db.get(InferenceAccountingOwner, "deployment")
            rows = list((await self.db.execute(select(InferenceCostReservation))).scalars())
            if account is None:
                if rows or lifecycle.get("inference_accounting") is not None or (prior and prior.get("witness") is not None):
                    raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
                accounting = {}
            else:
                witness = _witness(account)
                if (account.ledger_digest != _ledger_digest(account, rows)
                    or lifecycle.get("inference_accounting") != witness
                    or prior is None or prior.get("witness") != witness
                    or prior.get("secret_values_included") is not False
                    or not isinstance(prior.get("account"), dict)):
                    raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
                accounting = {"base": prior.get("base"), "account": prior["account"],
                    "operations": [], "witness": witness, "secret_values_included": False}
        if target == self.base and not self.db.info.get("composition_accounting_payload"):
            self.prepared_commit = True
            return
        if len(delta) + len(accounting.get("operations", [])) > 128:
            raise ProductionWorkspaceReconciliationError("composition_transaction_delta_exceeded")
        checkpoint = {**accounting, "schema_version": 2, "composition_base": self.base,
            "composition_target": target, "composition_delta": delta, "secret_values_included": False}
        receipt = read_lifecycle_receipt(self.workspace) or {"secret_values_included": False}
        receipt["runtime_composition"] = target
        if accounting.get("witness") is not None:
            receipt["inference_accounting"] = accounting["witness"]
        # Check BOTH size bounds before publishing either file.
        from src.workspace.production import MAX_ACCOUNTING_CHECKPOINT_BYTES, MAX_LIFECYCLE_RECEIPT_BYTES
        if (len(json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode()) > MAX_ACCOUNTING_CHECKPOINT_BYTES
            or len(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()) > MAX_LIFECYCLE_RECEIPT_BYTES):
            raise ProductionWorkspaceReconciliationError("composition_publication_size_exceeded")
        write_accounting_checkpoint(self.workspace, checkpoint)
        write_lifecycle_receipt(self.workspace, receipt, _accounting_lock_held=True)
        self.prepared_commit = True

    def close(self):
        from sqlalchemy import event
        for name, callback in self.listeners:
            event.remove(self.db.sync_session, name, callback)
        for connection, callback in self.connection_listeners:
            event.remove(connection, "before_cursor_execute", callback)
        _HELD_COMPOSITION_WORKSPACE.reset(self.token)
        self.lock.__exit__(None, None, None)


async def prepare_composition_session(db, *, fresh=False):
    """Acquire external ownership BEFORE any native SQL writer starts."""
    from sqlalchemy import text
    from config.settings import settings
    if db.in_transaction():
        raise ProductionWorkspaceReconciliationError("composition_lock_before_writer_required")
    exists = await db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_composition_states'"))
    populated = await db.scalar(text("SELECT 1 FROM runtime_composition_states LIMIT 1")) if exists else None
    await db.rollback()
    if not populated and not fresh:
        return None
    lock = maintenance_accounting_lock(Path(settings.workspace_dir).resolve())
    workspace = lock.__enter__()
    try:
        connection = await db.connection()
        base, members = await connection.run_sync(lambda conn: composition_closure(conn, verify_files=native_composition_files))
        receipt = read_lifecycle_receipt(workspace) or {}
        expected = receipt.get("runtime_composition")
        if base != expected or (fresh and (base is not None or expected is not None)):
            raise ProductionWorkspaceReconciliationError("composition_continuity_unavailable")
        from src.workspace.production import read_accounting_checkpoint
        checkpoint = read_accounting_checkpoint(workspace)
        if checkpoint and checkpoint.get("schema_version") == 2 and checkpoint.get("composition_target") != base:
            raise ProductionWorkspaceReconciliationError("composition_pending_checkpoint_requires_reconciliation")
        await db.rollback()
        read_guard = db.info.pop("composition_read_guard", None)
        if read_guard is not None:
            read_guard.close()
        guard = CompositionSessionGuard(db, workspace, lock, base, members)
        db.info["composition_guard"] = guard
        db.info["composition_base_witness"] = base
        guard.install()
        return guard
    except BaseException:
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


def assert_deployment_binding(workspace):
    directory = workspace.lifecycle_directory
    if (not os.environ.get(LIFECYCLE_PATH_ENV) and workspace.lifecycle_path is None
        or not directory.is_absolute() or directory.resolve() != directory or directory.is_relative_to(workspace.host_root)):
        raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
    receipt = read_lifecycle_receipt(workspace) or {}
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
def maintenance_accounting_lock(root, *, require_binding=True):
    workspace = root if isinstance(root, ProductionWorkspace) else ProductionWorkspace(host_root=root)
    if require_binding:
        assert_deployment_binding(workspace)
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
