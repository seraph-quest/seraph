"""Original sandboxed REPORT Source and observation-only pre-effect denial.

UNRUN candidate. A failed sandbox/terminal owner is a failure, never fake proof.
Numeric totals are partial lower bounds, not full-U or effect permission.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


def _save_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    assert path.stat().st_mode & 0o777 == 0o600


async def _terminal_report(sessions, operator, monkeypatch, root):
    from playwright.async_api import async_playwright
    from config.settings import settings
    from src.browser import task_runner
    from src.browser.pinned_transport import PinnedBrowserTransport
    from src.security.site_policy import SiteAccessDecision
    from src.goals.repository import GoalRepository
    from src.goals.contracts import GoalAdmissionBudget
    from src.conversation.task_context import TaskContinuityService, ContinueTaskRequest
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate, WorkBoardInputArtifactCreate
    from src.work_board.repository import WorkBoardRepository
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.work_board.pipeline_contracts import PipelinePreviewRequest, PipelineAcceptRequest
    from src.work_board import pipelines
    from src.work_board.pipeline_cpu import read_output
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.workflows.job_runtime import DurableJobRepository
    from src.db.models import Session, WorkBoardAttempt, WorkBoardStatus, WorkflowRunState
    from src.memory.m5 import _require_original_m5_session
    from src.runtime_plugins.ownership import begin_native_writer
    from src.work_board.input_artifacts import _commit_artifact_writer
    from tests.test_browser_task_runtime import _input
    from tests.test_browser_task_lifecycle import _html_responses

    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    monkeypatch.setattr(settings, "browser_site_allowlist", "fixture.example")
    monkeypatch.setattr(settings, "browser_site_blocklist", "")
    goal = await GoalRepository().create(title="Original bounded terminal REPORT",
        owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
        admission_budget=GoalAdmissionBudget(reviewed_grant=True, grant_id="review-914",
            max_outstanding_jobs=1, max_attempts=2, max_runtime_seconds=300))
    board = WorkBoardRepository()
    continuity = TaskContinuityService(board)
    await continuity.start()
    try:
        async with sessions() as db:
            await begin_native_writer(db, owner="native_ingress")
            task = (await board.create_task(db, owner, WorkBoardTaskCreate(
                title="Original task conversation", goal_id=goal.id, goal_revision=goal.revision,
                idempotency_key="numeric-prefix-original-conversation"), origin_session_id=owner.session_id)).task
            await _commit_artifact_writer(db)
        async with sessions() as db:
            assert await db.get(Session, owner.session_id) is None
            await db.rollback()
            reply = await continuity.continue_task(db, operator, ContinueTaskRequest(
                task_id=task.task_id, expected_revision=task.task_revision,
                new_conversation_id=owner.session_id))
            assert reply["conversation_id"] == owner.session_id and reply["idempotent_replay"] is False
            await _commit_artifact_writer(db)
            await _require_original_m5_session(db, owner.principal_id, owner.session_id)
    finally:
        await continuity.stop()
    # Original physical input owner and pipeline accept; no terminal SQL seeds.
    async with sessions() as db:
        await begin_native_writer(db, owner="native_ingress")
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id="browser.public-task.v1", goal_id=goal.id,
            goal_revision=goal.revision, input=_input(), idempotency_key="numeric-prefix-source-input"))
        await begin_native_writer(db, owner="native_ingress")
        producer = (await board.create_task(db, owner, WorkBoardTaskCreate(
            title="Finite public reference", goal_id=goal.id, goal_revision=goal.revision,
            capability_id="browser.public-task.v1", input_artifact_id=artifact.artifact_id,
            status=WorkBoardStatus.todo, priority=87, idempotency_key="numeric-prefix-source-task"))).task
        producer_id, producer_revision = producer.task_id, producer.task_revision
        await _commit_artifact_writer(db)
        await begin_native_writer(db, owner="native_ingress")
        preview = await pipelines.preview(db, owner, producer_id, PipelinePreviewRequest(
            expected_revision=producer_revision, source_input_artifact_id=artifact.artifact_id,
            idempotency_key="numeric-prefix-operation"))
        await _commit_artifact_writer(db)
        operation = await pipelines.accept(db, owner, preview["operation_id"], PipelineAcceptRequest(
            expected_revision=preview["revision"], expected_parent_revision=preview["parent_revision"],
            expected_digest=preview["digest"]))
        await _commit_artifact_writer(db)
    responses = _html_responses()
    fetches = []
    async def fetch(request):
        fetches.append(1)
        return responses[request.url]
    async def policy(url, **kwargs):
        return SiteAccessDecision(allowed=url in responses, hostname="fixture.example")
    async def resolver(*args):
        return ["93.184.216.34"]
    original_runner = task_runner.BrowserTaskRunner
    jobs = DurableJobRepository()
    dispatcher = WorkBoardDispatcher(repository=board, jobs=jobs, session_provider=sessions)
    # Observe the original callback without substituting its authority or
    # repeating its queries. It suppresses dependency errors as False, so keep
    # only bounded scalar diagnostics from calls it actually makes.
    original_current = dispatcher._browser_assert_current
    async def observe_current(**binding):
        from contextlib import asynccontextmanager
        from sqlalchemy import event
        from src.work_board import dispatcher as dispatcher_module
        from src.work_board import input_artifacts as artifact_module
        from src.guardian import opportunity_plans as opportunity_module
        observations = {}
        from src.memory import composition_headers as observed_headers
        from src.memory.header_bounds import COMPOSITION_DESCRIPTORS
        diagnostic_source_tokens = ('.', '0', '0123456789abcdef', '1', 'BINARY', 'BLOB', 'BOOLEAN', 'DATETIME', 'FLOAT', 'INTEGER', 'NULL', 'UTF-8', 'VARCHAR', '_', '__dict__', '__eq__', '__getattr__', '__getattribute__', '__init__', '__mro__', '__new__', '__table__', '__tablename__', '_common_numeric_trace', '_compiler_dispatch', '_complete_owner', '_completion_capacity_task', '_create_events', '_default_length', '_delivered_response', '_header_budget', '_issued_id', '_member_map_', '_member_names_', '_name_', '_native_memory_affected_numeric_facts', '_native_memory_effect_numeric_schedule', '_native_memory_integer_slots', '_native_memory_normalize_numeric_appearance', '_native_memory_resolve_numeric_slot', '_native_memory_retention_numeric_facts', '_native_memory_source_capacity_facts', '_native_memory_source_common_numeric', '_native_memory_source_retention', '_native_programme_start_numeric_entries', '_native_programme_start_numeric_owner', '_numeric_reservation', '_omit_aliases', '_render_string_type', '_retention', '_rowid_', '_sort_key_function', '_value_', '_variant_mapping', 'absences', 'acceptance_binding_digest', 'accepted', 'accepted_at', 'accepted_by_principal_id', 'accepted_by_session_id', 'accepted_memory_content_digest', 'accepted_memory_id', 'accepting', 'active', 'actor', 'admission', 'admission_budget_json', 'admission_job_id', 'admission_status', 'after_action_id', 'after_begin', 'after_input_digest', 'after_selected_capability_id', 'alembic_version', 'aliases_json', 'approval_requests', 'archived', 'artifact_digest', 'artifact_id', 'artifact_ref', 'asdecimal', 'attempt_id', 'audio_consent_grants', 'audio_ingress_jobs', 'audit_events', 'authority-model', 'authority_digest', 'awaiting_owner_confirmation', 'baseline', 'before_action_id', 'before_cursor_execute', 'before_input_digest', 'before_selected_capability_id', 'binding_json', 'bindings', 'block', 'blocked', 'body', 'brief_bytes', 'budget', 'c', 'c0', 'c1', 'c2', 'c3', 'c4', 'c5', 'calendar_event_bindings', 'calendar_prep_receipts', 'calendar_read_consents', 'calendar_reschedule_consents', 'candidate_set_digest', 'canonical_bound_not_certified', 'canonical_key', 'canonical_name', 'capability_id', 'capability_version', 'capture', 'category', 'changed', 'checkpoint_id', 'claimed', 'closed', 'collaborator', 'collation', 'collector-list', 'colspecs', 'columns', 'commitment', 'common-certification-trace', 'common_issuer_numeric_collector', 'communication_preference', 'comparison_context_digest', 'compile', 'complete-headers', 'complete-schema', 'complete-schema-flat-construction', 'complete-schema-fts-decoding', 'completion', 'composition_guard', 'composition_header_bytes', 'composition_read_guard', 'confidence', 'confirmation_binding_digest', 'confirmed', 'confirmed_action_id', 'confirmed_at', 'confirmer_principal_id', 'confirmer_session_id', 'connection', 'connection-state', 'consumed', 'consumed_at', 'content', 'contradicts', 'conversation', 'cookies', 'corrects_memory_id', 'count', 'create_constraint', 'created_at', 'decision', 'decision_effect', 'decision_status', 'delivered', 'delivery', 'delivery-construction', 'denied', 'dependency_id', 'description', 'descriptor', 'descriptor-compiler', 'descriptor-input-result', 'descriptor-schema', 'descriptor-table', 'descriptor-xinfo', 'discovery-full-continuation-reservation', 'discovery-physical', 'discovery-staging-reservation', 'domain', 'done', 'due_date', 'edge_type', 'effect', 'effect_id_digest', 'embedding_id', 'encoded-copy', 'encoded_bytes', 'encoding', 'enroll', 'entity_type', 'entries', 'entry-list-slot', 'entry-pair', 'entry_key', 'entry_type', 'enum_class', 'enums', 'episode_type', 'estimated_cost', 'event_id', 'evidence_digest', 'evidence_use_snapshot_json', 'exact-construction', 'exact-header', 'exact-metadata', 'exact-xinfo-callback', 'exact-xinfo-controls', 'exec_driver_sql', 'expired', 'expires_at', 'fact', 'facts-copy', 'family', 'final-state', 'finite_service', 'forgejo_connections', 'from_memory_id', 'fts-columns', 'fts-index', 'fts-index-parts', 'fts-indexes', 'fts-layout', 'fts-schema', 'fts-sql', 'fts-sql-body', 'fts-sql-header', 'github_followthrough_connections', 'goal', 'goal_id', 'goal_programmes_json', 'goal_revision', 'goals', 'google_service_connections', 'governed_schedule_bindings', 'governed_schedule_occurrences', 'grant_revision', 'guardian_decision_packets', 'guardian_inbox_actions', 'guardian_inbox_dispositions', 'guardian_interventions', 'guardian_opportunities', 'guardian_policy_json', 'guardian_policy_revision', 'guardian_routine_versions', 'guardian_routines', 'guardian_source_baselines', 'guardian_source_watches', 'handoff_id', 'header-fact-copy', 'header_certificate_stale', 'header_certificate_unavailable', 'header_descriptor_unavailable', 'header_encoding_unsupported', 'header_fts_metadata_changed', 'header_future_reference_duplicate', 'header_json_bound', 'header_json_invalid', 'header_locator_changed', 'header_locator_unsupported', 'header_metadata_bound', 'header_metadata_owner_unavailable', 'header_metadata_raw_owner_unavailable', 'header_metadata_transaction_required', 'header_raw_owner_unavailable', 'header_reference_bound', 'header_request_bound', 'header_row_unavailable', 'header_rowid_unavailable', 'header_scalar_type_unsupported', 'header_scalar_unavailable', 'header_schema_changed', 'header_schema_cookie_changed', 'header_schema_cookie_unavailable', 'header_schema_object_bound', 'header_schema_object_unavailable', 'header_schema_source_unavailable', 'header_snapshot_changed', 'header_sqlite_transaction_required', 'header_sqlite_version_unsupported', 'header_transaction_required', 'id', 'idempotency_key', 'identity-indexes', 'identity-objects', 'identity-parts', 'ids', 'impl', 'impl_instance', 'importance', 'index', 'index-construction', 'index_columns', 'indexes', 'inference_accounting_owners', 'inference_cost_reservations', 'inherit_schema', 'initial-state', 'input-reader', 'input_digest', 'integer', 'intent_id', 'issued_id', 'ix_', 'ix_approval_requests_action', 'ix_approval_requests_causation_id', 'ix_approval_requests_channel', 'ix_approval_requests_conversation_id', 'ix_approval_requests_correlation_id', 'ix_approval_requests_created_at', 'ix_approval_requests_device_id', 'ix_approval_requests_expires_at', 'ix_approval_requests_fingerprint', 'ix_approval_requests_operator_session_id', 'ix_approval_requests_owner_principal_id', 'ix_approval_requests_risk_level', 'ix_approval_requests_session_id', 'ix_approval_requests_status', 'ix_approval_requests_thread_id', 'ix_approval_requests_tool_name', 'ix_approval_requests_transport', 'ix_audio_consent_grants_boundary', 'ix_audio_consent_grants_created_at', 'ix_audio_consent_grants_expires_at', 'ix_audio_consent_grants_granted_at', 'ix_audio_consent_grants_operator_session_id', 'ix_audio_consent_grants_owner_principal_id', 'ix_audio_consent_grants_reference', 'ix_audio_consent_grants_revoked_at', 'ix_audio_consent_grants_state', 'ix_audio_consent_grants_updated_at', 'ix_audio_ingress_jobs_admission_operation_id', 'ix_audio_ingress_jobs_attachment_id', 'ix_audio_ingress_jobs_audio_payload_digest', 'ix_audio_ingress_jobs_captured_at', 'ix_audio_ingress_jobs_cleanup_status', 'ix_audio_ingress_jobs_confirmed_transcript_digest', 'ix_audio_ingress_jobs_created_at', 'ix_audio_ingress_jobs_error_code', 'ix_audio_ingress_jobs_message_id', 'ix_audio_ingress_jobs_operator_session_id', 'ix_audio_ingress_jobs_owner_principal_id', 'ix_audio_ingress_jobs_provider_status', 'ix_audio_ingress_jobs_raw_audio_retention_deadline', 'ix_audio_ingress_jobs_request_digest', 'ix_audio_ingress_jobs_request_id', 'ix_audio_ingress_jobs_requested_capability', 'ix_audio_ingress_jobs_result_digest', 'ix_audio_ingress_jobs_session_id', 'ix_audio_ingress_jobs_status', 'ix_audio_ingress_jobs_transcript_digest', 'ix_audio_ingress_jobs_transport_lease_id', 'ix_audio_ingress_jobs_transport_status', 'ix_audio_ingress_jobs_updated_at', 'ix_audit_events_actor', 'ix_audit_events_created_at', 'ix_audit_events_event_type', 'ix_audit_events_policy_mode', 'ix_audit_events_risk_level', 'ix_audit_events_session_id', 'ix_audit_events_tool_name', 'ix_calendar_event_bindings_calendar_list_revision', 'ix_calendar_event_bindings_connection_id', 'ix_calendar_event_bindings_connection_revision', 'ix_calendar_event_bindings_consent_id', 'ix_calendar_event_bindings_consent_revision', 'ix_calendar_event_bindings_created_at', 'ix_calendar_event_bindings_event_key', 'ix_calendar_event_bindings_event_revision', 'ix_calendar_event_bindings_fetched_at', 'ix_calendar_event_bindings_owner_event', 'ix_calendar_event_bindings_owner_principal_id', 'ix_calendar_event_bindings_owner_session_id', 'ix_calendar_event_bindings_provider_identity_digest', 'ix_calendar_event_bindings_revision', 'ix_calendar_event_bindings_snapshot_digest', 'ix_calendar_event_bindings_state', 'ix_calendar_event_bindings_updated_at', 'ix_calendar_prep_receipts_artifact_id', 'ix_calendar_prep_receipts_attempt_id', 'ix_calendar_prep_receipts_connection_id', 'ix_calendar_prep_receipts_connection_revision', 'ix_calendar_prep_receipts_consent_id', 'ix_calendar_prep_receipts_consent_revision', 'ix_calendar_prep_receipts_content_sha256', 'ix_calendar_prep_receipts_created_at', 'ix_calendar_prep_receipts_durable_job_id', 'ix_calendar_prep_receipts_event_binding_id', 'ix_calendar_prep_receipts_event_key', 'ix_calendar_prep_receipts_expires_at', 'ix_calendar_prep_receipts_failure_code', 'ix_calendar_prep_receipts_goal_id', 'ix_calendar_prep_receipts_goal_revision', 'ix_calendar_prep_receipts_memory_status', 'ix_calendar_prep_receipts_owner', 'ix_calendar_prep_receipts_owner_principal_id', 'ix_calendar_prep_receipts_owner_session_id', 'ix_calendar_prep_receipts_readback_id', 'ix_calendar_prep_receipts_status', 'ix_calendar_prep_receipts_task', 'ix_calendar_prep_receipts_task_id', 'ix_calendar_prep_receipts_updated_at', 'ix_calendar_read_consents_connection', 'ix_calendar_read_consents_connection_id', 'ix_calendar_read_consents_connection_revision', 'ix_calendar_read_consents_consent_digest', 'ix_calendar_read_consents_created_at', 'ix_calendar_read_consents_creation_request_digest', 'ix_calendar_read_consents_expires_at', 'ix_calendar_read_consents_goal_id', 'ix_calendar_read_consents_goal_revision', 'ix_calendar_read_consents_owner_principal_id', 'ix_calendar_read_consents_owner_session_id', 'ix_calendar_read_consents_owner_state', 'ix_calendar_read_consents_revision', 'ix_calendar_read_consents_state', 'ix_calendar_read_consents_updated_at', 'ix_calendar_reschedule_consents_event_binding_id', 'ix_calendar_reschedule_consents_expires_at', 'ix_calendar_reschedule_consents_goal_id', 'ix_calendar_reschedule_consents_original_root_session_id', 'ix_calendar_reschedule_consents_owner_principal_id', 'ix_calendar_reschedule_consents_read_connection_id', 'ix_calendar_reschedule_consents_state', 'ix_calendar_reschedule_consents_write_connection_id', 'ix_forgejo_connections_owner_principal_id', 'ix_forgejo_connections_owner_session_id', 'ix_github_followthrough_connections_active_fence', 'ix_github_followthrough_connections_active_job_id', 'ix_github_followthrough_connections_created_at', 'ix_github_followthrough_connections_mode', 'ix_github_followthrough_connections_owner_principal_id', 'ix_github_followthrough_connections_revision', 'ix_github_followthrough_connections_updated_at', 'ix_goals_domain', 'ix_goals_owner_principal_id', 'ix_goals_owner_session_id', 'ix_goals_parent_id', 'ix_goals_proactive_enabled', 'ix_goals_revision', 'ix_goals_status', 'ix_google_service_connections_created_at', 'ix_google_service_connections_credential_fingerprint', 'ix_google_service_connections_owner_principal_id', 'ix_google_service_connections_owner_session_id', 'ix_google_service_connections_owner_state', 'ix_google_service_connections_revision', 'ix_google_service_connections_revoke_idempotency_key', 'ix_google_service_connections_revoke_request_digest', 'ix_google_service_connections_scope_status', 'ix_google_service_connections_service', 'ix_google_service_connections_setup_request_digest', 'ix_google_service_connections_state', 'ix_google_service_connections_updated_at', 'ix_google_service_connections_vault_secret_key', 'ix_google_service_connections_verified_setup_job_id', 'ix_governed_schedule_bindings_action_digest', 'ix_governed_schedule_bindings_action_type', 'ix_governed_schedule_bindings_binding_revision', 'ix_governed_schedule_bindings_cadence_kind', 'ix_governed_schedule_bindings_capability_id', 'ix_governed_schedule_bindings_consent_digest', 'ix_governed_schedule_bindings_consent_kind', 'ix_governed_schedule_bindings_consent_revision', 'ix_governed_schedule_bindings_created_at', 'ix_governed_schedule_bindings_expires_at', 'ix_governed_schedule_bindings_goal_id', 'ix_governed_schedule_bindings_goal_revision', 'ix_governed_schedule_bindings_input_artifact_id', 'ix_governed_schedule_bindings_input_digest', 'ix_governed_schedule_bindings_last_slot_utc', 'ix_governed_schedule_bindings_owner_principal_id', 'ix_governed_schedule_bindings_owner_session_id', 'ix_governed_schedule_bindings_owner_state', 'ix_governed_schedule_bindings_read_consent_id', 'ix_governed_schedule_bindings_schedule_request_digest', 'ix_governed_schedule_bindings_scheduled_job_id', 'ix_governed_schedule_bindings_state', 'ix_governed_schedule_bindings_updated_at', 'ix_governed_schedule_occurrences_binding_id', 'ix_governed_schedule_occurrences_binding_revision', 'ix_governed_schedule_occurrences_binding_slot', 'ix_governed_schedule_occurrences_created_at', 'ix_governed_schedule_occurrences_durable_job_id', 'ix_governed_schedule_occurrences_idempotency_key', 'ix_governed_schedule_occurrences_lease_expires_at', 'ix_governed_schedule_occurrences_request_digest', 'ix_governed_schedule_occurrences_slot_utc', 'ix_governed_schedule_occurrences_state', 'ix_governed_schedule_occurrences_updated_at', 'ix_governed_schedule_occurrences_work_board_task_id', 'ix_guardian_decision_packets_approval_id', 'ix_guardian_decision_packets_created_at', 'ix_guardian_decision_packets_criteria_digest', 'ix_guardian_decision_packets_dossier_artifact_id', 'ix_guardian_decision_packets_dossier_sha256', 'ix_guardian_decision_packets_failure_code', 'ix_guardian_decision_packets_goal_id', 'ix_guardian_decision_packets_goal_revision', 'ix_guardian_decision_packets_inbox_pending_updated', 'ix_guardian_decision_packets_input_digest', 'ix_guardian_decision_packets_memory_status', 'ix_guardian_decision_packets_observed_checkpoint_sha256', 'ix_guardian_decision_packets_plan_revision', 'ix_guardian_decision_packets_run_identity', 'ix_guardian_decision_packets_source_watch_id', 'ix_guardian_decision_packets_status', 'ix_guardian_decision_packets_strategy_delta_id', 'ix_guardian_decision_packets_task_artifact_id', 'ix_guardian_decision_packets_task_sha256', 'ix_guardian_decision_packets_verification_status', 'ix_guardian_decision_packets_watch_id', 'ix_guardian_inbox_actions_action', 'ix_guardian_inbox_actions_created_at', 'ix_guardian_inbox_actions_idempotency_key', 'ix_guardian_inbox_actions_item', 'ix_guardian_inbox_actions_item_id', 'ix_guardian_inbox_actions_owner_principal_id', 'ix_guardian_inbox_actions_owner_session_id', 'ix_guardian_inbox_actions_payload_digest', 'ix_guardian_inbox_actions_task_id', 'ix_guardian_inbox_dispositions_created_at', 'ix_guardian_inbox_dispositions_expires_at', 'ix_guardian_inbox_dispositions_goal_id', 'ix_guardian_inbox_dispositions_goal_revision', 'ix_guardian_inbox_dispositions_last_action_receipt_id', 'ix_guardian_inbox_dispositions_owner_principal_id', 'ix_guardian_inbox_dispositions_owner_session_id', 'ix_guardian_inbox_dispositions_owner_state', 'ix_guardian_inbox_dispositions_plan_revision', 'ix_guardian_inbox_dispositions_revision', 'ix_guardian_inbox_dispositions_snoozed_until', 'ix_guardian_inbox_dispositions_source_digest', 'ix_guardian_inbox_dispositions_source_id', 'ix_guardian_inbox_dispositions_source_kind', 'ix_guardian_inbox_dispositions_state', 'ix_guardian_inbox_dispositions_task_id', 'ix_guardian_inbox_dispositions_updated_at', 'ix_guardian_inbox_dispositions_watch_id', 'ix_guardian_interventions_active_project', 'ix_guardian_interventions_created_at', 'ix_guardian_interventions_data_quality', 'ix_guardian_interventions_delivery_decision', 'ix_guardian_interventions_feedback_at', 'ix_guardian_interventions_feedback_type', 'ix_guardian_interventions_guardian_confidence', 'ix_guardian_interventions_interruption_mode', 'ix_guardian_interventions_intervention_type', 'ix_guardian_interventions_is_scheduled', 'ix_guardian_interventions_latest_outcome', 'ix_guardian_interventions_message_type', 'ix_guardian_interventions_notification_id', 'ix_guardian_interventions_opportunity_feedback', 'ix_guardian_interventions_policy_action', 'ix_guardian_interventions_session_id', 'ix_guardian_interventions_transport', 'ix_guardian_interventions_updated_at', 'ix_guardian_interventions_urgency', 'ix_guardian_interventions_user_state', 'ix_guardian_opportunity_owner_goal_created', 'ix_guardian_opportunity_status_created', 'ix_guardian_routine_versions_created_at', 'ix_guardian_routine_versions_installed_at', 'ix_guardian_routine_versions_installed_package_digest', 'ix_guardian_routine_versions_routine_id', 'ix_guardian_routine_versions_runbook_sha256', 'ix_guardian_routine_versions_version', 'ix_guardian_routine_versions_workflow_sha256', 'ix_guardian_routines_current_version', 'ix_guardian_routines_name', 'ix_guardian_routines_owner_principal_id', 'ix_guardian_routines_owner_session_id', 'ix_guardian_routines_revision', 'ix_guardian_routines_state', 'ix_guardian_source_baselines_baseline_sha256', 'ix_guardian_source_baselines_identity_digest', 'ix_guardian_source_baselines_last_error_code', 'ix_guardian_source_baselines_observed_at', 'ix_guardian_source_baselines_source_key', 'ix_guardian_source_baselines_state', 'ix_guardian_source_baselines_watch_id', 'ix_guardian_source_watches_active_job_fence', 'ix_guardian_source_watches_active_job_id', 'ix_guardian_source_watches_capability_id', 'ix_guardian_source_watches_capability_version', 'ix_guardian_source_watches_criteria_digest', 'ix_guardian_source_watches_goal_id', 'ix_guardian_source_watches_goal_revision', 'ix_guardian_source_watches_last_error_code', 'ix_guardian_source_watches_last_run_identity', 'ix_guardian_source_watches_last_status', 'ix_guardian_source_watches_owner_principal_id', 'ix_guardian_source_watches_owner_session_id', 'ix_guardian_source_watches_plan_revision', 'ix_guardian_source_watches_scheduled_job_id', 'ix_guardian_source_watches_source_set_digest', 'ix_guardian_source_watches_state', 'ix_guardian_source_watches_write_mode', 'ix_inference_cost_reservations_deployment_id', 'ix_inference_cost_reservations_goal_id', 'ix_inference_cost_reservations_job_id', 'ix_inference_cost_reservations_owner_group_lookup', 'ix_inference_cost_reservations_owner_id', 'ix_inference_cost_reservations_period_id', 'ix_inference_cost_reservations_sequence', 'ix_inference_cost_reservations_state', 'ix_mail_label_bindings_connection_id', 'ix_mail_label_bindings_connection_revision', 'ix_mail_label_bindings_created_at', 'ix_mail_label_bindings_owner_connection', 'ix_mail_label_bindings_owner_principal_id', 'ix_mail_label_bindings_owner_session_id', 'ix_mail_label_bindings_provider_label_digest', 'ix_mail_label_bindings_revision', 'ix_mail_label_bindings_state', 'ix_mail_label_bindings_updated_at', 'ix_mail_message_bindings_connection_id', 'ix_mail_message_bindings_connection_revision', 'ix_mail_message_bindings_created_at', 'ix_mail_message_bindings_fetched_at', 'ix_mail_message_bindings_message_key', 'ix_mail_message_bindings_message_revision', 'ix_mail_message_bindings_owner_principal_id', 'ix_mail_message_bindings_owner_session_id', 'ix_mail_message_bindings_owner_state', 'ix_mail_message_bindings_received_at', 'ix_mail_message_bindings_revision', 'ix_mail_message_bindings_source_consent_id', 'ix_mail_message_bindings_source_consent_revision', 'ix_mail_message_bindings_source_label_scope_digest', 'ix_mail_message_bindings_status', 'ix_mail_message_bindings_thread_key', 'ix_mail_message_bindings_updated_at', 'ix_mail_read_consents_connection', 'ix_mail_read_consents_connection_id', 'ix_mail_read_consents_connection_revision', 'ix_mail_read_consents_created_at', 'ix_mail_read_consents_creation_request_digest', 'ix_mail_read_consents_expires_at', 'ix_mail_read_consents_goal_id', 'ix_mail_read_consents_goal_revision', 'ix_mail_read_consents_max_messages', 'ix_mail_read_consents_model_digest', 'ix_mail_read_consents_model_egress_allowed', 'ix_mail_read_consents_model_reviewed_at', 'ix_mail_read_consents_model_revision', 'ix_mail_read_consents_owner_principal_id', 'ix_mail_read_consents_owner_session_id', 'ix_mail_read_consents_owner_state', 'ix_mail_read_consents_revision', 'ix_mail_read_consents_revoke_idempotency_key', 'ix_mail_read_consents_revoke_request_digest', 'ix_mail_read_consents_source_digest', 'ix_mail_read_consents_source_read_allowed', 'ix_mail_read_consents_source_reviewed_at', 'ix_mail_read_consents_source_revision', 'ix_mail_read_consents_state', 'ix_mail_read_consents_updated_at', 'ix_mail_read_consents_window_days', 'ix_mail_watch_states_baseline_complete', 'ix_mail_watch_states_connection_id', 'ix_mail_watch_states_connection_revision', 'ix_mail_watch_states_consent_id', 'ix_mail_watch_states_created_at', 'ix_mail_watch_states_goal_id', 'ix_mail_watch_states_goal_revision', 'ix_mail_watch_states_last_completed_occurrence_id', 'ix_mail_watch_states_last_observed_at', 'ix_mail_watch_states_list_fetched_at', 'ix_mail_watch_states_list_page_complete', 'ix_mail_watch_states_owner_principal_id', 'ix_mail_watch_states_owner_session_id', 'ix_mail_watch_states_owner_state', 'ix_mail_watch_states_revision', 'ix_mail_watch_states_seen_message_keys_digest', 'ix_mail_watch_states_skipped_coverage_reason', 'ix_mail_watch_states_source_consent_revision', 'ix_mail_watch_states_state', 'ix_mail_watch_states_updated_at', 'ix_mail_watch_states_window_end_utc', 'ix_mail_watch_states_window_start_utc', 'ix_memories_kind', 'ix_memories_kind_scope_key_unique', 'ix_memories_project_entity_id', 'ix_memories_scope_key', 'ix_memories_status', 'ix_memories_subject_entity_id', 'ix_memory_edges_edge_type', 'ix_memory_edges_from_memory_id', 'ix_memory_edges_to_memory_id', 'ix_memory_entities_canonical_key', 'ix_memory_entities_canonical_name', 'ix_memory_entities_entity_type', 'ix_memory_episodes_created_at', 'ix_memory_episodes_episode_type', 'ix_memory_episodes_observed_at', 'ix_memory_episodes_project_entity_id', 'ix_memory_episodes_session_id', 'ix_memory_episodes_source_message_id', 'ix_memory_episodes_source_role', 'ix_memory_episodes_source_tool_name', 'ix_memory_episodes_subject_entity_id', 'ix_memory_proposals_accepted_at', 'ix_memory_proposals_accepted_by_principal_id', 'ix_memory_proposals_accepted_by_session_id', 'ix_memory_proposals_accepted_memory_content_digest', 'ix_memory_proposals_accepted_memory_id', 'ix_memory_proposals_artifact_digest', 'ix_memory_proposals_artifact_ref', 'ix_memory_proposals_candidate_set_digest', 'ix_memory_proposals_capability_id', 'ix_memory_proposals_capability_version', 'ix_memory_proposals_corrects_memory_id', 'ix_memory_proposals_created_at', 'ix_memory_proposals_decision_effect', 'ix_memory_proposals_evidence_digest', 'ix_memory_proposals_exact_comparison', 'ix_memory_proposals_expires_at', 'ix_memory_proposals_goal_id', 'ix_memory_proposals_goal_revision', 'ix_memory_proposals_memory_kind', 'ix_memory_proposals_owner_principal_id', 'ix_memory_proposals_owner_session_id', 'ix_memory_proposals_preview_text_digest', 'ix_memory_proposals_privacy_state', 'ix_memory_proposals_proposal_job_id', 'ix_memory_proposals_provider_contact_count', 'ix_memory_proposals_provider_contact_started', 'ix_memory_proposals_provider_contact_state', 'ix_memory_proposals_readback_digest', 'ix_memory_proposals_reason_code', 'ix_memory_proposals_recovered_from_proposal_id', 'ix_memory_proposals_recovery_action', 'ix_memory_proposals_rejected_at', 'ix_memory_proposals_rejected_by_principal_id', 'ix_memory_proposals_rejected_by_session_id', 'ix_memory_proposals_request_binding_digest', 'ix_memory_proposals_request_idempotency_key', 'ix_memory_proposals_revision', 'ix_memory_proposals_rollback_at', 'ix_memory_proposals_rollback_by_principal_id', 'ix_memory_proposals_rollback_by_session_id', 'ix_memory_proposals_schema_version', 'ix_memory_proposals_source_attempt_fence', 'ix_memory_proposals_source_attempt_id', 'ix_memory_proposals_source_context_digest', 'ix_memory_proposals_source_task_id', 'ix_memory_proposals_source_task_revision', 'ix_memory_proposals_status', 'ix_memory_proposals_typed_input_digest', 'ix_memory_proposals_updated_at', 'ix_memory_proposals_workflow_run_id', 'ix_memory_proposals_workflow_run_revision', 'ix_memory_snapshots_canonical_tombstone_revision', 'ix_memory_snapshots_kind', 'ix_memory_sources_memory_id', 'ix_memory_sources_source_message_id', 'ix_memory_sources_source_session_id', 'ix_memory_sources_source_type', 'ix_memory_tombstones_actor', 'ix_memory_tombstones_created_at', 'ix_memory_tombstones_memory_id', 'ix_messages_causation_id', 'ix_messages_channel', 'ix_messages_conversation_id', 'ix_messages_correlation_id', 'ix_messages_device_id', 'ix_messages_operator_session_id', 'ix_messages_owner_principal_id', 'ix_messages_role', 'ix_messages_session_id', 'ix_messages_thread_id', 'ix_messages_transport', 'ix_model_capability_proofs_adapter', 'ix_model_capability_proofs_binding', 'ix_model_capability_proofs_capability', 'ix_model_capability_proofs_checked_at', 'ix_model_capability_proofs_created_at', 'ix_model_capability_proofs_endpoint_class', 'ix_model_capability_proofs_endpoint_digest', 'ix_model_capability_proofs_expires_at', 'ix_model_capability_proofs_model', 'ix_model_capability_proofs_outcome', 'ix_model_capability_proofs_profile_contract_hash', 'ix_model_capability_proofs_profile_id', 'ix_model_capability_proofs_profile_schema_version', 'ix_model_capability_proofs_proof_hash', 'ix_model_capability_proofs_receipt_hash', 'ix_model_capability_proofs_receipt_id', 'ix_model_route_attempt_receipts_attempt_id', 'ix_model_route_attempt_receipts_created_at', 'ix_model_route_attempt_receipts_endpoint_digest', 'ix_model_route_attempt_receipts_outcome', 'ix_model_route_attempt_receipts_profile_id', 'ix_model_route_attempt_receipts_route_receipt_id', 'ix_model_route_receipts_actual_profile_id', 'ix_model_route_receipts_created_at', 'ix_model_route_receipts_egress_class', 'ix_model_route_receipts_finished_at', 'ix_model_route_receipts_outcome', 'ix_model_route_receipts_receipt_hash', 'ix_model_route_receipts_receipt_id', 'ix_model_route_receipts_request_id', 'ix_model_route_receipts_route_decision_id', 'ix_model_route_receipts_runtime_path', 'ix_model_route_receipts_started_at', 'ix_model_route_receipts_workload', 'ix_model_route_receipts_workload_success', 'ix_moltbook_connections_owner_principal_id', 'ix_moltbook_connections_owner_session_id', 'ix_native_notification_delivery_attempts_attempt_index', 'ix_native_notification_delivery_attempts_error_code', 'ix_native_notification_delivery_attempts_fencing_token', 'ix_native_notification_delivery_attempts_finished_at', 'ix_native_notification_delivery_attempts_lease_owner', 'ix_native_notification_delivery_attempts_notification_id', 'ix_native_notification_delivery_attempts_started_at', 'ix_native_notification_delivery_attempts_status', 'ix_native_notification_outbox_attempt_count', 'ix_native_notification_outbox_budget_limit', 'ix_native_notification_outbox_budget_period_key', 'ix_native_notification_outbox_cancelled_at', 'ix_native_notification_outbox_causation_id', 'ix_native_notification_outbox_channel', 'ix_native_notification_outbox_continuation_mode', 'ix_native_notification_outbox_conversation_id', 'ix_native_notification_outbox_correlation_id', 'ix_native_notification_outbox_created_at', 'ix_native_notification_outbox_deadline_at', 'ix_native_notification_outbox_degraded_state', 'ix_native_notification_outbox_delivered_at', 'ix_native_notification_outbox_device_id', 'ix_native_notification_outbox_fencing_token', 'ix_native_notification_outbox_goal_id', 'ix_native_notification_outbox_goal_revision', 'ix_native_notification_outbox_idempotency_key', 'ix_native_notification_outbox_intervention_id', 'ix_native_notification_outbox_intervention_type', 'ix_native_notification_outbox_last_error', 'ix_native_notification_outbox_lease', 'ix_native_notification_outbox_lease_expires_at', 'ix_native_notification_outbox_lease_owner', 'ix_native_notification_outbox_max_attempts', 'ix_native_notification_outbox_operator_session_id', 'ix_native_notification_outbox_owner_principal_id', 'ix_native_notification_outbox_payload_digest', 'ix_native_notification_outbox_pending_order', 'ix_native_notification_outbox_session_id', 'ix_native_notification_outbox_status', 'ix_native_notification_outbox_surface', 'ix_native_notification_outbox_thread_id', 'ix_native_notification_outbox_thread_source', 'ix_native_notification_outbox_transport', 'ix_native_notification_outbox_updated_at', 'ix_native_notification_outbox_urgency', 'ix_operator_continuity_credentials_identity_id', 'ix_operator_continuity_credentials_kind', 'ix_operator_continuity_credentials_token_hash', 'ix_operator_recovery_journals_current_session_id', 'ix_operator_recovery_journals_identity_id', 'ix_operator_recovery_journals_state', 'ix_operator_sessions_absolute_expires_at', 'ix_operator_sessions_created_at', 'ix_operator_sessions_idle_expires_at', 'ix_operator_sessions_is_bearer_tombstone', 'ix_operator_sessions_last_seen_at', 'ix_operator_sessions_operator_identity_id', 'ix_operator_sessions_principal_id', 'ix_operator_sessions_replaced_by_id', 'ix_operator_sessions_replacement_state', 'ix_operator_sessions_revoked_at', 'ix_operator_sessions_token_hash', 'ix_paired_edge_artifacts_artifact_id', 'ix_paired_edge_artifacts_captured_at', 'ix_paired_edge_artifacts_content_hash', 'ix_paired_edge_artifacts_created_at', 'ix_paired_edge_artifacts_device_id', 'ix_paired_edge_artifacts_extension_id', 'ix_paired_edge_artifacts_owner_created', 'ix_paired_edge_artifacts_owner_principal_id', 'ix_paired_edge_artifacts_pairing_id', 'ix_paired_edge_artifacts_reference', 'ix_paired_edge_artifacts_request_id', 'ix_paired_edge_artifacts_sequence', 'ix_procedure_v2_bindings_created_at', 'ix_procedure_v2_bindings_deterministic_routine_id', 'ix_procedure_v2_bindings_idempotency_key', 'ix_procedure_v2_bindings_owner_principal_id', 'ix_procedure_v2_bindings_owner_session_id', 'ix_procedure_v2_bindings_preview_digest', 'ix_procedure_v2_bindings_preview_expires_at', 'ix_procedure_v2_bindings_recovery_reason', 'ix_procedure_v2_bindings_request_digest', 'ix_procedure_v2_bindings_revision', 'ix_procedure_v2_bindings_state', 'ix_procedure_v2_bindings_template_id', 'ix_procedure_v2_bindings_updated_at', 'ix_procedure_v2_bindings_version_id', 'ix_production_workflow_authority_states_blocked_replay_reason', 'ix_production_workflow_authority_states_created_at', 'ix_production_workflow_authority_states_lease_revision', 'ix_production_workflow_authority_states_resumable_step_state', 'ix_production_workflow_authority_states_run_identity', 'ix_production_workflow_authority_states_safe_replay_decision', 'ix_production_workflow_authority_states_scheduler_state_owner', 'ix_production_workflow_authority_states_side_effect_status', 'ix_production_workflow_authority_states_updated_at', 'ix_production_workflow_authority_states_worker_owner', 'ix_production_workflow_authority_states_workflow_lease_id', 'ix_production_workflow_authority_states_workflow_name', 'ix_production_workflow_authority_states_workflow_phase', 'ix_production_workflow_fault_receipts_campaign_window', 'ix_production_workflow_fault_receipts_created_at', 'ix_production_workflow_fault_receipts_fault_key', 'ix_production_workflow_fault_receipts_injection_method', 'ix_production_workflow_fault_receipts_operator_intervention_required', 'ix_production_workflow_fault_receipts_raw_receipt_handle', 'ix_production_workflow_fault_receipts_recovery_result', 'ix_production_workflow_fault_receipts_replay_decision', 'ix_production_workflow_fault_receipts_run_identity', 'ix_production_workflow_fault_receipts_updated_at', 'ix_production_workflow_side_effect_receipts_created_at', 'ix_production_workflow_side_effect_receipts_external_confirmation_state', 'ix_production_workflow_side_effect_receipts_idempotency_key', 'ix_production_workflow_side_effect_receipts_idempotency_scope', 'ix_production_workflow_side_effect_receipts_manual_repair_state', 'ix_production_workflow_side_effect_receipts_operator_replay_decision', 'ix_production_workflow_side_effect_receipts_reconciliation_id', 'ix_production_workflow_side_effect_receipts_reconciliation_outcome', 'ix_production_workflow_side_effect_receipts_redacted_receipt_handle', 'ix_production_workflow_side_effect_receipts_run_identity', 'ix_production_workflow_side_effect_receipts_side_effect_kind', 'ix_production_workflow_side_effect_receipts_updated_at', 'ix_programme_digest_receipts_local_date', 'ix_programme_digest_receipts_owner_identity_id', 'ix_programme_finding_actions_finding_id', 'ix_programme_finding_actions_local_date', 'ix_programme_finding_actions_owner_identity_id', 'ix_programme_follow_through_finding_id', 'ix_programme_follow_through_owner_identity_id', 'ix_queued_insights_budget_limit', 'ix_queued_insights_budget_period_key', 'ix_queued_insights_goal_id', 'ix_queued_insights_goal_revision', 'ix_queued_insights_intervention_id', 'ix_queued_insights_operator_session_id', 'ix_queued_insights_owner_principal_id', 'ix_queued_insights_session_id', 'ix_repo_repair_egress_consents_consent_digest', 'ix_repo_repair_egress_consents_created_at', 'ix_repo_repair_egress_consents_effective_profile_id', 'ix_repo_repair_egress_consents_effective_upstream', 'ix_repo_repair_egress_consents_expires_at', 'ix_repo_repair_egress_consents_goal_id', 'ix_repo_repair_egress_consents_goal_revision', 'ix_repo_repair_egress_consents_input_digest', 'ix_repo_repair_egress_consents_job_state', 'ix_repo_repair_egress_consents_owner_principal_id', 'ix_repo_repair_egress_consents_owner_session_id', 'ix_repo_repair_egress_consents_request_digest', 'ix_repo_repair_egress_consents_request_key', 'ix_repo_repair_egress_consents_revision', 'ix_repo_repair_egress_consents_runtime_path', 'ix_repo_repair_egress_consents_source_digest', 'ix_repo_repair_egress_consents_source_manifest_digest', 'ix_repo_repair_egress_consents_source_packet_id', 'ix_repo_repair_egress_consents_state', 'ix_repo_repair_egress_consents_updated_at', 'ix_repo_repair_egress_consents_work_board_attempt_id', 'ix_repo_repair_egress_consents_work_board_task_id', 'ix_repo_repair_egress_consents_workflow_run_id', 'ix_repo_repair_proposals_approval_fingerprint', 'ix_repo_repair_proposals_approval_id', 'ix_repo_repair_proposals_authority_digest', 'ix_repo_repair_proposals_base_snapshot_digest', 'ix_repo_repair_proposals_created_at', 'ix_repo_repair_proposals_expires_at', 'ix_repo_repair_proposals_goal_id', 'ix_repo_repair_proposals_goal_revision', 'ix_repo_repair_proposals_last_receipt_id', 'ix_repo_repair_proposals_model_output_digest', 'ix_repo_repair_proposals_model_profile_id', 'ix_repo_repair_proposals_model_request_digest', 'ix_repo_repair_proposals_model_response_artifact_id', 'ix_repo_repair_proposals_model_response_artifact_sha256', 'ix_repo_repair_proposals_model_runtime_path', 'ix_repo_repair_proposals_operation_key', 'ix_repo_repair_proposals_owner_principal_id', 'ix_repo_repair_proposals_owner_session_id', 'ix_repo_repair_proposals_owner_status', 'ix_repo_repair_proposals_patch_artifact_id', 'ix_repo_repair_proposals_patch_sha256', 'ix_repo_repair_proposals_repository_ref', 'ix_repo_repair_proposals_request_digest', 'ix_repo_repair_proposals_revision', 'ix_repo_repair_proposals_source_digest', 'ix_repo_repair_proposals_source_packet_id', 'ix_repo_repair_proposals_status', 'ix_repo_repair_proposals_updated_at', 'ix_repo_repair_proposals_work_board_attempt_id', 'ix_repo_repair_proposals_work_board_task_id', 'ix_repo_repair_proposals_workflow_run_id', 'ix_repo_repair_source_packets_artifact_id', 'ix_repo_repair_source_packets_artifact_sha256', 'ix_repo_repair_source_packets_base_snapshot_digest', 'ix_repo_repair_source_packets_created_at', 'ix_repo_repair_source_packets_goal_id', 'ix_repo_repair_source_packets_goal_revision', 'ix_repo_repair_source_packets_input_digest', 'ix_repo_repair_source_packets_owner_principal_id', 'ix_repo_repair_source_packets_owner_session_id', 'ix_repo_repair_source_packets_owner_state', 'ix_repo_repair_source_packets_repository_ref', 'ix_repo_repair_source_packets_revision', 'ix_repo_repair_source_packets_source_manifest_digest', 'ix_repo_repair_source_packets_state', 'ix_repo_repair_source_packets_updated_at', 'ix_repo_repair_source_packets_work_board_attempt_id', 'ix_repo_repair_source_packets_work_board_task_id', 'ix_repo_repair_source_packets_workflow_run_id', 'ix_scheduled_job_runs_action_type', 'ix_scheduled_job_runs_approval_id', 'ix_scheduled_job_runs_created_by_session_id', 'ix_scheduled_job_runs_finished_at', 'ix_scheduled_job_runs_outcome', 'ix_scheduled_job_runs_scheduled_job_id', 'ix_scheduled_job_runs_session_id', 'ix_scheduled_job_runs_started_at', 'ix_scheduled_job_runs_status', 'ix_scheduled_job_runs_trigger_type', 'ix_scheduled_jobs_action_type', 'ix_scheduled_jobs_created_by_session_id', 'ix_scheduled_jobs_enabled', 'ix_scheduled_jobs_last_approval_id', 'ix_scheduled_jobs_last_outcome', 'ix_scheduled_jobs_last_run_at', 'ix_scheduled_jobs_session_id', 'ix_scheduled_jobs_trigger_type', 'ix_screen_observations_activity_type', 'ix_screen_observations_app_name', 'ix_screen_observations_project', 'ix_screen_observations_timestamp', 'ix_secrets_key', 'ix_secrets_owner_principal_id', 'ix_secrets_revoked_at', 'ix_session_todos_completed', 'ix_session_todos_session_id', 'ix_session_todos_sort_order', 'ix_sessions_continuity_task_id', 'ix_sessions_owner_principal_id', 'ix_strategy_deltas_created_at', 'ix_strategy_deltas_field_name', 'ix_strategy_deltas_goal_id', 'ix_strategy_deltas_goal_revision_after', 'ix_strategy_deltas_goal_revision_before', 'ix_strategy_deltas_rollback_target_id', 'ix_strategy_deltas_scope', 'ix_strategy_deltas_source_event_id', 'ix_strategy_deltas_status', 'ix_task_method_active_goal_id', 'ix_task_method_active_owner_identity_id', 'ix_telegram_delivery_attempts_attempt_index', 'ix_telegram_delivery_attempts_error_code', 'ix_telegram_delivery_attempts_fencing_token', 'ix_telegram_delivery_attempts_finished_at', 'ix_telegram_delivery_attempts_lease_owner', 'ix_telegram_delivery_attempts_outbox_id', 'ix_telegram_delivery_attempts_response_code', 'ix_telegram_delivery_attempts_started_at', 'ix_telegram_delivery_attempts_status', 'ix_telegram_inbound_updates_attachment_hash', 'ix_telegram_inbound_updates_attachment_id', 'ix_telegram_inbound_updates_attachment_quarantine_receipt_digest', 'ix_telegram_inbound_updates_canonical_message_id', 'ix_telegram_inbound_updates_chat_id', 'ix_telegram_inbound_updates_content_digest', 'ix_telegram_inbound_updates_created_at', 'ix_telegram_inbound_updates_idempotency_key', 'ix_telegram_inbound_updates_message_id', 'ix_telegram_inbound_updates_operator_id', 'ix_telegram_inbound_updates_operator_session_id', 'ix_telegram_inbound_updates_owner_principal_id', 'ix_telegram_inbound_updates_reason_code', 'ix_telegram_inbound_updates_request_digest', 'ix_telegram_inbound_updates_sequence', 'ix_telegram_inbound_updates_session_id', 'ix_telegram_inbound_updates_status', 'ix_telegram_inbound_updates_update_id', 'ix_telegram_task_callbacks_consumed_at', 'ix_telegram_task_callbacks_created_at', 'ix_telegram_task_callbacks_expires_at', 'ix_telegram_task_callbacks_nonce_digest', 'ix_telegram_task_callbacks_operator_session_id', 'ix_telegram_task_callbacks_outbox_id', 'ix_telegram_task_callbacks_owner_principal_id', 'ix_telegram_task_callbacks_pairing_id', 'ix_telegram_task_callbacks_query_id', 'ix_telegram_task_callbacks_status', 'ix_telegram_task_callbacks_task_id', 'ix_telegram_transport_outbox_attempt_count', 'ix_telegram_transport_outbox_cancelled_at', 'ix_telegram_transport_outbox_chat_id', 'ix_telegram_transport_outbox_content_digest', 'ix_telegram_transport_outbox_conversation_id', 'ix_telegram_transport_outbox_correlation_id', 'ix_telegram_transport_outbox_created_at', 'ix_telegram_transport_outbox_deadline_at', 'ix_telegram_transport_outbox_delivered_at', 'ix_telegram_transport_outbox_external_message_id', 'ix_telegram_transport_outbox_fencing_token', 'ix_telegram_transport_outbox_idempotency_key', 'ix_telegram_transport_outbox_kind', 'ix_telegram_transport_outbox_last_error', 'ix_telegram_transport_outbox_lease_expires_at', 'ix_telegram_transport_outbox_lease_owner', 'ix_telegram_transport_outbox_max_attempts', 'ix_telegram_transport_outbox_message_id', 'ix_telegram_transport_outbox_next_attempt_at', 'ix_telegram_transport_outbox_operator_session_id', 'ix_telegram_transport_outbox_owner_principal_id', 'ix_telegram_transport_outbox_payload_digest', 'ix_telegram_transport_outbox_response_code', 'ix_telegram_transport_outbox_session_id', 'ix_telegram_transport_outbox_status', 'ix_telegram_transport_outbox_thread_id', 'ix_telegram_transport_outbox_updated_at', 'ix_telegram_transport_states_chat_id', 'ix_telegram_transport_states_cursor', 'ix_telegram_transport_states_last_error', 'ix_telegram_transport_states_last_update_at', 'ix_telegram_transport_states_model_consent_expires_at', 'ix_telegram_transport_states_model_consent_reference', 'ix_telegram_transport_states_operator_id', 'ix_telegram_transport_states_operator_session_id', 'ix_telegram_transport_states_owner_principal_id', 'ix_telegram_transport_states_pairing_expires_at', 'ix_telegram_transport_states_pairing_id', 'ix_telegram_transport_states_pairing_state', 'ix_telegram_transport_states_revoked_at', 'ix_telegram_transport_states_sequence', 'ix_telegram_transport_states_token_fingerprint', 'ix_telegram_transport_states_token_secret_ref', 'ix_telegram_transport_states_transit_consent_expires_at', 'ix_telegram_transport_states_transit_consent_reference', 'ix_telegram_transport_states_updated_at', 'ix_work_board_attempts_cancel_requested_at', 'ix_work_board_attempts_created_at', 'ix_work_board_attempts_ended_at', 'ix_work_board_attempts_executor_id', 'ix_work_board_attempts_fencing_token', 'ix_work_board_attempts_heartbeat_at', 'ix_work_board_attempts_lease_expires_at', 'ix_work_board_attempts_lease_owner', 'ix_work_board_attempts_outcome', 'ix_work_board_attempts_started_at', 'ix_work_board_attempts_task_id', 'ix_work_board_attempts_task_revision_at_claim', 'ix_work_board_attempts_updated_at', 'ix_work_board_attempts_workflow_run_id', 'ix_work_board_comments_author_principal_id', 'ix_work_board_comments_author_session_id', 'ix_work_board_comments_created_at', 'ix_work_board_comments_owner_principal_id', 'ix_work_board_comments_owner_session_id', 'ix_work_board_comments_task_id', 'ix_work_board_decision_receipts_accepted_memory_content_digest', 'ix_work_board_decision_receipts_accepted_memory_id', 'ix_work_board_decision_receipts_admission_status', 'ix_work_board_decision_receipts_after_input_digest', 'ix_work_board_decision_receipts_after_selected_capability_id', 'ix_work_board_decision_receipts_before_input_digest', 'ix_work_board_decision_receipts_before_selected_capability_id', 'ix_work_board_decision_receipts_capability_id', 'ix_work_board_decision_receipts_capability_version', 'ix_work_board_decision_receipts_comparison_context_digest', 'ix_work_board_decision_receipts_confirmation_binding_digest', 'ix_work_board_decision_receipts_confirmed_at', 'ix_work_board_decision_receipts_confirmer_principal_id', 'ix_work_board_decision_receipts_confirmer_session_id', 'ix_work_board_decision_receipts_consumed_at', 'ix_work_board_decision_receipts_created_at', 'ix_work_board_decision_receipts_decision_status', 'ix_work_board_decision_receipts_exact_intent', 'ix_work_board_decision_receipts_goal_id', 'ix_work_board_decision_receipts_goal_revision', 'ix_work_board_decision_receipts_later_attempt_fence', 'ix_work_board_decision_receipts_later_attempt_id', 'ix_work_board_decision_receipts_later_task_id', 'ix_work_board_decision_receipts_later_task_revision', 'ix_work_board_decision_receipts_later_workflow_run_id', 'ix_work_board_decision_receipts_owner_principal_id', 'ix_work_board_decision_receipts_owner_session_id', 'ix_work_board_decision_receipts_receipt_binding_digest', 'ix_work_board_decision_receipts_receipt_stage', 'ix_work_board_decision_receipts_revision', 'ix_work_board_decision_receipts_schema_version', 'ix_work_board_decision_receipts_source_attempt_fence', 'ix_work_board_decision_receipts_source_attempt_id', 'ix_work_board_decision_receipts_source_baseline_receipt_id', 'ix_work_board_decision_receipts_source_context_digest', 'ix_work_board_decision_receipts_source_proposal_id', 'ix_work_board_decision_receipts_source_proposal_revision', 'ix_work_board_decision_receipts_source_task_id', 'ix_work_board_decision_receipts_source_task_revision', 'ix_work_board_decision_receipts_source_workflow_run_id', 'ix_work_board_decision_receipts_source_workflow_run_revision', 'ix_work_board_decision_receipts_task_intent_digest', 'ix_work_board_decision_receipts_typed_input_digest', 'ix_work_board_decision_receipts_updated_at', 'ix_work_board_events_actor_principal_id', 'ix_work_board_events_actor_session_id', 'ix_work_board_events_created_at', 'ix_work_board_events_kind', 'ix_work_board_events_owner_principal_id', 'ix_work_board_events_owner_session_id', 'ix_work_board_events_task_id', 'ix_work_board_evidence_dependencies_goal_id', 'ix_work_board_evidence_dependencies_owner_principal_id', 'ix_work_board_evidence_dependencies_owner_session_id', 'ix_work_board_evidence_dependencies_task_id', 'ix_work_board_evidence_owner_source', 'ix_work_board_handoffs_child_task_id', 'ix_work_board_handoffs_created_at', 'ix_work_board_handoffs_link_id', 'ix_work_board_handoffs_owner_principal_id', 'ix_work_board_handoffs_owner_session_id', 'ix_work_board_handoffs_parent_task_id', 'ix_work_board_handoffs_schema_version', 'ix_work_board_handoffs_source_attempt_id', 'ix_work_board_handoffs_source_task_revision', 'ix_work_board_handoffs_workflow_run_id', 'ix_work_board_input_artifacts_bound_task_id', 'ix_work_board_input_artifacts_bound_task_revision', 'ix_work_board_input_artifacts_capability_id', 'ix_work_board_input_artifacts_capability_version', 'ix_work_board_input_artifacts_consumed_at', 'ix_work_board_input_artifacts_created_at', 'ix_work_board_input_artifacts_expires_at', 'ix_work_board_input_artifacts_goal_id', 'ix_work_board_input_artifacts_goal_revision', 'ix_work_board_input_artifacts_idempotency_key', 'ix_work_board_input_artifacts_metadata_digest', 'ix_work_board_input_artifacts_owner_principal_id', 'ix_work_board_input_artifacts_owner_session_id', 'ix_work_board_input_artifacts_payload', 'ix_work_board_input_artifacts_payload_sha256', 'ix_work_board_input_artifacts_revision', 'ix_work_board_input_artifacts_state', 'ix_work_board_input_artifacts_state_expiry', 'ix_work_board_input_artifacts_typed_input_ref', 'ix_work_board_links_child_task_id', 'ix_work_board_links_created_at', 'ix_work_board_links_current_handoff_id', 'ix_work_board_links_owner_principal_id', 'ix_work_board_links_owner_session_id', 'ix_work_board_links_parent_task_id', 'ix_work_board_proposals_admission_job_id', 'ix_work_board_proposals_authority_digest', 'ix_work_board_proposals_capability_id', 'ix_work_board_proposals_capability_version', 'ix_work_board_proposals_created_at', 'ix_work_board_proposals_effect_id_digest', 'ix_work_board_proposals_expires_at', 'ix_work_board_proposals_goal_revision', 'ix_work_board_proposals_grant_revision', 'ix_work_board_proposals_idempotency_key', 'ix_work_board_proposals_input_digest', 'ix_work_board_proposals_kind', 'ix_work_board_proposals_owner_principal_id', 'ix_work_board_proposals_owner_session_id', 'ix_work_board_proposals_parent_revision', 'ix_work_board_proposals_parent_task_id', 'ix_work_board_proposals_proposal_digest', 'ix_work_board_proposals_provider_contact_started', 'ix_work_board_proposals_provider_contact_state', 'ix_work_board_proposals_request_digest', 'ix_work_board_proposals_revision', 'ix_work_board_proposals_route_id', 'ix_work_board_proposals_status', 'ix_work_board_review_intents_attempt_id', 'ix_work_board_review_intents_created_at', 'ix_work_board_review_intents_fencing_token', 'ix_work_board_review_intents_owner_principal_id', 'ix_work_board_review_intents_owner_session_id', 'ix_work_board_review_intents_request_digest', 'ix_work_board_review_intents_status', 'ix_work_board_review_intents_task_id', 'ix_work_board_review_intents_task_revision', 'ix_work_board_review_intents_workflow_run_id', 'ix_work_board_routine_bindings_action_task_id', 'ix_work_board_routine_bindings_created_at', 'ix_work_board_routine_bindings_deterministic_routine_id', 'ix_work_board_routine_bindings_idempotency_key', 'ix_work_board_routine_bindings_install_job_id', 'ix_work_board_routine_bindings_owner_principal_id', 'ix_work_board_routine_bindings_owner_session_id', 'ix_work_board_routine_bindings_preview_digest', 'ix_work_board_routine_bindings_revision', 'ix_work_board_routine_bindings_routine_id', 'ix_work_board_routine_bindings_source_task_id', 'ix_work_board_routine_bindings_state', 'ix_work_board_routine_bindings_updated_at', 'ix_work_board_tasks_archived_at', 'ix_work_board_tasks_assignee_id', 'ix_work_board_tasks_block_kind', 'ix_work_board_tasks_block_source_status', 'ix_work_board_tasks_capability_id', 'ix_work_board_tasks_completed_at', 'ix_work_board_tasks_created_at', 'ix_work_board_tasks_executor_id', 'ix_work_board_tasks_goal_id', 'ix_work_board_tasks_goal_revision', 'ix_work_board_tasks_idempotency_binding', 'ix_work_board_tasks_idempotency_key', 'ix_work_board_tasks_idempotency_payload_digest', 'ix_work_board_tasks_idempotency_scope', 'ix_work_board_tasks_input_artifact_id', 'ix_work_board_tasks_origin_session_id', 'ix_work_board_tasks_origin_thread_id', 'ix_work_board_tasks_owner_principal_id', 'ix_work_board_tasks_owner_session_id', 'ix_work_board_tasks_pipeline_operation_id', 'ix_work_board_tasks_pipeline_slot', 'ix_work_board_tasks_priority', 'ix_work_board_tasks_ready_order', 'ix_work_board_tasks_requires_review', 'ix_work_board_tasks_review_expires_at', 'ix_work_board_tasks_review_request_attempt_id', 'ix_work_board_tasks_review_request_digest', 'ix_work_board_tasks_review_request_fence', 'ix_work_board_tasks_review_request_revision', 'ix_work_board_tasks_review_requested_at', 'ix_work_board_tasks_reviewer_id', 'ix_work_board_tasks_scheduled_at', 'ix_work_board_tasks_status', 'ix_work_board_tasks_task_id', 'ix_work_board_tasks_task_revision', 'ix_work_board_tasks_typed_input_digest', 'ix_work_board_tasks_typed_input_ref', 'ix_work_board_tasks_updated_at', 'ix_workflow_artifact_reviews_approval_id', 'ix_workflow_artifact_reviews_artifact_path', 'ix_workflow_artifact_reviews_created_at', 'ix_workflow_artifact_reviews_decided_at', 'ix_workflow_artifact_reviews_owner', 'ix_workflow_artifact_reviews_parent_run_identity', 'ix_workflow_artifact_reviews_review_state', 'ix_workflow_artifact_reviews_reviewer', 'ix_workflow_artifact_reviews_root_run_identity', 'ix_workflow_artifact_reviews_run_identity', 'ix_workflow_artifact_reviews_updated_at', 'ix_workflow_artifact_reviews_workflow_name', 'ix_workflow_run_states_attempt_count', 'ix_workflow_run_states_authority_digest', 'ix_workflow_run_states_branch_kind', 'ix_workflow_run_states_budget_digest', 'ix_workflow_run_states_candidate_id', 'ix_workflow_run_states_capability_version', 'ix_workflow_run_states_conversation_id', 'ix_workflow_run_states_deadline_at', 'ix_workflow_run_states_failure_reason', 'ix_workflow_run_states_fencing_token', 'ix_workflow_run_states_finished_at', 'ix_workflow_run_states_goal_id', 'ix_workflow_run_states_goal_revision', 'ix_workflow_run_states_heartbeat_at', 'ix_workflow_run_states_idempotency_binding', 'ix_workflow_run_states_idempotency_key', 'ix_workflow_run_states_idempotency_scope', 'ix_workflow_run_states_input_digest', 'ix_workflow_run_states_job_kind', 'ix_workflow_run_states_last_completed_step_id', 'ix_workflow_run_states_lease_expires_at', 'ix_workflow_run_states_lease_owner', 'ix_workflow_run_states_max_attempts', 'ix_workflow_run_states_operator_session_id', 'ix_workflow_run_states_owner_kind', 'ix_workflow_run_states_owner_principal_id', 'ix_workflow_run_states_parent_fencing_token', 'ix_workflow_run_states_parent_job_id', 'ix_workflow_run_states_parent_run_identity', 'ix_workflow_run_states_plan_revision', 'ix_workflow_run_states_priority', 'ix_workflow_run_states_record_schema_version', 'ix_workflow_run_states_revision', 'ix_workflow_run_states_root_run_identity', 'ix_workflow_run_states_run_fingerprint', 'ix_workflow_run_states_run_identity', 'ix_workflow_run_states_selected_context_quota', 'ix_workflow_run_states_service_id', 'ix_workflow_run_states_session_id', 'ix_workflow_run_states_source_task', 'ix_workflow_run_states_started_at', 'ix_workflow_run_states_status', 'ix_workflow_run_states_tool_name', 'ix_workflow_run_states_updated_at', 'ix_workflow_run_states_workflow_name', 'ix_workflow_step_states_completed_at', 'ix_workflow_step_states_run_identity', 'ix_workflow_step_states_started_at', 'ix_workflow_step_states_status', 'ix_workflow_step_states_step_id', 'ix_workflow_step_states_step_index', 'ix_workflow_step_states_tool_name', 'ix_workflow_step_states_updated_at', 'ix_workflow_step_states_workflow_name', 'job_id', 'k', 'key', 'kind', 'last_confirmed_at', 'later_attempt_fence', 'later_attempt_id', 'later_comparison', 'later_task_id', 'later_task_revision', 'later_workflow_run_id', 'ledger-storage', 'length', 'level', 'limit', 'link_id', 'load_dialect_impl', 'locator-construction', 'locator-indexes', 'locator-metadata', 'locator-parts', 'locator-request', 'mail_label_bindings', 'mail_message_bindings', 'mail_read_consents', 'mail_watch_states', 'materialization', 'memories', 'memory_common_numeric_trace_unavailable', 'memory_complete_numeric_forecast_unavailable', 'memory_edges', 'memory_entities', 'memory_episodes', 'memory_future_header_complete_trace_unavailable', 'memory_future_header_locator_order_unavailable', 'memory_future_header_source_changed', 'memory_future_header_source_unavailable', 'memory_future_integer_slot_changed', 'memory_header_exact_schema_owner_unavailable', 'memory_header_logical_index_dimensions_unavailable', 'memory_header_logical_input_unavailable', 'memory_id', 'memory_kind', 'memory_numeric_appearance_unreserved', 'memory_numeric_forecast_owner_changed', 'memory_numeric_ledger_certificate_changed', 'memory_numeric_ledger_charge_exceeded', 'memory_numeric_ledger_charge_invalid', 'memory_numeric_ledger_entry_invalid', 'memory_numeric_ledger_phase_invalid', 'memory_numeric_ledger_plan_changed', 'memory_numeric_ledger_storage_owner_changed', 'memory_numeric_ledger_unavailable', 'memory_numeric_ledger_writer_changed', 'memory_numeric_projection_identity_changed', 'memory_numeric_projection_identity_unassigned', 'memory_numeric_projection_trace_invalid', 'memory_numeric_reference_shape_invalid', 'memory_numeric_scalar_unavailable', 'memory_numeric_wrs_shape_invalid', 'memory_preledger_validation_owner_changed', 'memory_preledger_validation_shape_changed', 'memory_proposals', 'memory_scope_json', 'memory_snapshot_scope_unavailable', 'memory_snapshots', 'memory_sources', 'memory_tombstones', 'memorycategory', 'memoryedgetype', 'memoryentitytype', 'memoryepisodetype', 'memorykind', 'memoryproposaldecisioneffect', 'memoryproposalprivacystate', 'memoryproposalprovidercontactstate', 'memoryproposalstatus', 'memorystatus', 'messages', 'metadata', 'metadata-input-result', 'metadata-prequery', 'metadata_json', 'model', 'model_capability_proofs', 'model_route_attempt_receipts', 'model_route_receipts', 'moltbook_connections', 'name', 'name-list-copy', 'name-set-copy', 'native-composition-memory.v1', 'native-memory-header-logical', 'native-memory-preledger', 'native-programme-physical', 'native_enum', 'native_memory_charge_trace', 'native_memory_numeric_phase', 'native_notification_delivery_attempts', 'native_notification_outbox', 'no_change', 'no_comparable', 'no_learning', 'none', 'not_required', 'not_started', 'null', 'numeric-construction', 'obligation', 'observer', 'oid', 'operation', 'operation_id', 'operator-token-index-list', 'operator-token-index-parts', 'operator-token-locator', 'operator-token-schema', 'operator_continuity_credentials', 'operator_identities', 'operator_principal_required_insert', 'operator_principal_required_update', 'operator_recovery_journals', 'operator_sessions', 'opportunity_id', 'opportunity_revision', 'ordinal-set-copy', 'ordinal-sorted-copy', 'ordinal-visits', 'organization', 'original-rows', 'original_checkpoint_digest', 'original_discovery_numeric_reads', 'original_memory_snapshot_scope', 'original_session_numeric_reads', 'own_upper', 'owner_events', 'owner_identity_id', 'owner_operation_kind', 'owner_principal_id', 'owner_session_id', 'paired_edge_artifacts', 'parent_id', 'parent_revision', 'parent_task_id', 'path', 'pattern', 'payload', 'pending_inference', 'person', 'pgno', 'pk', 'plan', 'plan_bytes', 'precision', 'preference', 'prepared', 'preparer', 'present', 'preview_text', 'preview_text_digest', 'previous_binding_json', 'privacy_state', 'proactive_enabled', 'procedural', 'procedure_v2_bindings', 'process', 'production_workflow_authority_states', 'production_workflow_fault_receipts', 'production_workflow_side_effect_receipts', 'profile', 'programme-identity-body', 'programme-identity-headers', 'programme-identity-index-parts', 'programme-identity-indexes', 'programme-identity-objects', 'programme-identity-prospective', 'programme-identity-schema', 'programme_constructor_numeric_inputs_changed', 'programme_constructor_rowid_bound', 'programme_copy_reservation_unavailable', 'programme_digest_receipts', 'programme_finding_actions', 'programme_follow_through', 'programme_identity_certificate_unavailable', 'programme_identity_locator_changed', 'programme_identity_owner_unavailable', 'programme_identity_schema_changed', 'programme_notification_preferences', 'programme_numeric_appearance_unreserved', 'programme_numeric_phase_unavailable', 'programme_numeric_reservation_owner_changed', 'programme_numeric_reservation_unavailable', 'programme_numeric_schedule_invalid', 'programme_numeric_stager_changed', 'programme_numeric_trace_changed', 'programme_source_identity_missing', 'programme_start_complete_numeric_forecast_unavailable', 'programme_start_numeric_appearance_unreserved', 'programme_start_numeric_delivery_unavailable', 'programme_start_numeric_entry_invalid', 'programme_start_numeric_owner_changed', 'programme_start_numeric_phase_invalid', 'programme_start_numeric_publication_unavailable', 'programme_start_numeric_writer_changed', 'programme_start_physical_graph_unavailable', 'programme_start_physical_numeric_facts_changed', 'project', 'project_entity_id', 'projection_revision', 'proposal_digest', 'proposal_id', 'proposal_job_id', 'proposal_json', 'proposed', 'provenance', 'provenance_json', 'provider_contact_count', 'provider_contact_started', 'provider_contact_state', 'public_brief', 'publication', 'publication-', 'publication-admission', 'publication-queue', 'queue', 'queued_insights', 'quote', 'rank', 'raw_owner', 'readback_digest', 'readback_kind', 'readback_ref', 'ready', 'real', 'reason', 'reason_code', 'receipt_binding_digest', 'receipt_id', 'receipt_integrity_mac', 'receipt_stage', 'records', 'recovered_from_proposal_id', 'recovery_action', 'redacted', 'ref', 'references', 'reflection', 'refs', 'reinforcement', 'rejected', 'rejected_at', 'rejected_by_principal_id', 'rejected_by_session_id', 'related', 'repo_repair_egress_consents', 'repo_repair_proposals', 'repo_repair_source_packets', 'request_binding_digest', 'request_digest', 'request_idempotency_key', 'require_operator_confirmation', 'research', 'resolve_future', 'retained-copy', 'retrieval_evidence_ids_json', 'return-copy', 'review', 'revision', 'revoked_at', 'rollback_at', 'rollback_by_principal_id', 'rollback_by_session_id', 'rollback_reason', 'rolled_back', 'route_id', 'routine', 'row-copy', 'row-header', 'row-locator', 'row-locator-input', 'row-locator-numeric', 'row_header_bytes', 'row_upper', 'row_uppers', 'rowid', 'rows', 'rows_digest', 'run_identity', 'running', 'runtime_composition_states', 'runtime_domain', 'safe', 'scalar-input-result', 'scalar_one', 'scheduled_job_runs', 'scheduled_jobs', 'schema', 'schema-construction', 'schema-cookies', 'schema-main', 'schema-temp', 'schema_version', 'scope', 'scope_key', 'screen_observations', 'seal', 'secrets', 'segid', 'selected_delta_digest', 'self', 'session-publication-reservation', 'session_id', 'session_recall_episodes_ad', 'session_recall_episodes_ai', 'session_recall_episodes_au', 'session_recall_fts', 'session_recall_fts_config', 'session_recall_fts_content', 'session_recall_fts_data', 'session_recall_fts_docsize', 'session_recall_fts_idx', 'session_recall_messages_ad', 'session_recall_messages_ai', 'session_recall_messages_au', 'session_recall_sessions_ad', 'session_recall_sessions_ai', 'session_recall_sessions_au', 'session_reservation_appearance_changed', 'session_reservation_cost_invalid', 'session_reservation_nested', 'session_reservation_owner_changed', 'session_todos', 'sessions', 'shadow', 'signature_key_id', 'signature_mac', 'skeleton-copy', 'snippet', 'sort_order', 'source', 'source-name-delivery', 'source_attempt_fence', 'source_attempt_id', 'source_baseline', 'source_baseline_receipt_id', 'source_context_digest', 'source_label', 'source_message_id', 'source_proposal_id', 'source_proposal_revision', 'source_refs_json', 'source_session_id', 'source_task_id', 'source_task_revision', 'source_type', 'source_workflow_run_id', 'source_workflow_run_revision', 'sqlite-encoding', 'sqlite-version', 'sqlite_autoindex_', 'sqlite_autoindex_operator_identities_1', 'sqlite_autoindex_session_recall_fts_config_1', 'sqlite_autoindex_session_recall_fts_idx_1', 'sqlite_autoindex_task_method_active_1', 'sqlite_autoindex_task_method_active_2', 'sqlite_sequence', 'staging', 'start', 'start_committed', 'start_date', 'started', 'state', 'state-changes', 'state-cookie-input', 'state_digest', 'status', 'storage-width-planning', 'strategy_deltas', 'subject_entity_id', 'succeeded', 'success_criterion_json', 'summary', 'superseded', 'supersedes', 'supports', 'sz', 'table', 'table-body', 'target-parameters', 'target-planning', 'target-sql-input', 'task-method-pointer', 'task-method-pointer-complete-header', 'task-method-pointer-foreign-existence', 'task-method-pointer-index-parts', 'task-method-pointer-indexes', 'task-method-pointer-layout', 'task-method-pointer-scope', 'task_id', 'task_intent_digest', 'task_method_active', 'task_method_pointer_ambiguous', 'task_method_pointer_locator_changed', 'task_method_pointer_owner_unavailable', 'task_method_pointer_schema_changed', 'telegram_delivery_attempts', 'telegram_inbound_updates', 'telegram_task_callbacks', 'telegram_transport_outbox', 'telegram_transport_states', 'term', 'text', 'thread', 'timeline', 'timezone', 'title', 'to_memory_id', 'todo', 'token-locator', 'token_hash', 'tool', 'triage', 'trigger', 'tuple_digest', 'type', 'type_compiler', 'type_compiler_cls', 'type_descriptor', 'type_engine', 'typed_input_digest', 'u', 'unknown', 'updated_at', 'user_profiles', 'utf-8', 'ux_', 'ux_audio_consent_grants_reference', 'ux_audio_ingress_jobs_request_id', 'ux_calendar_read_consents_creation_idempotency', 'ux_calendar_reschedule_active_grant', 'ux_forgejo_connection_owner', 'ux_github_followthrough_connections_owner', 'ux_guardian_decision_packets_watch_input', 'ux_guardian_inbox_actions_idempotency', 'ux_guardian_inbox_dispositions_source', 'ux_guardian_opportunity_owner_dedupe', 'ux_guardian_routine_versions_routine_version', 'ux_guardian_source_baselines_watch_source', 'ux_guardian_source_watches_scheduled_job', 'ux_mail_read_consents_creation_idempotency', 'ux_memory_proposals_owner_attempt_no_learning', 'ux_memory_proposals_owner_attempt_preview', 'ux_model_route_attempt_receipts_order', 'ux_moltbook_connection_owner', 'ux_native_notification_delivery_attempt_order', 'ux_operator_recovery_identity_key', 'ux_paired_edge_artifacts_pairing_request', 'ux_procedure_v2_bindings_deterministic_routine', 'ux_procedure_v2_bindings_idempotency', 'ux_programme_digest_owner_day', 'ux_programme_finding_action_owner_key', 'ux_programme_followthrough_owner_finding', 'ux_repo_repair_egress_consents_owner_request', 'ux_repo_repair_proposals_owner_operation', 'ux_repo_repair_source_packets_job_input', 'ux_strategy_deltas_source_event_id', 'ux_telegram_delivery_attempt_order', 'ux_work_board_attempts_active_task', 'ux_work_board_attempts_workflow_run', 'ux_work_board_decision_receipts_binding', 'ux_work_board_events_mutation_key', 'ux_work_board_handoffs_version', 'ux_work_board_input_artifacts_idempotency', 'ux_work_board_proposals_idempotency', 'ux_work_board_proposals_opportunity', 'ux_work_board_review_intents_binding', 'ux_work_board_routine_bindings_deterministic_routine', 'ux_work_board_routine_bindings_idempotency', 'ux_work_board_tasks_idempotency', 'ux_workflow_run_states_idempotency_binding', 'v', 'validate_strings', 'validated', 'validation', 'validation-constructor', 'validation-state', 'values_callable', 'virtual', 'visible', 'visit_BOOLEAN', 'visit_DATETIME', 'visit_FLOAT', 'visit_INTEGER', 'visit_VARCHAR', 'visit_boolean', 'visit_datetime', 'visit_enum', 'visit_float', 'visit_integer', 'visit_string', 'visit_type_decorator', 'weight', 'widths', 'work_board_attempts', 'work_board_comments', 'work_board_decision_receipts', 'work_board_events', 'work_board_evidence_dependencies', 'work_board_handoffs', 'work_board_input_artifacts', 'work_board_links', 'work_board_proposals', 'work_board_review_intents', 'work_board_routine_bindings', 'work_board_tasks', 'workboarddecisionadmissionstatus', 'workboarddecisionreceiptstage', 'workboarddecisionstatus', 'workboardstatus', 'workflow', 'workflow_artifact_reviews', 'workflow_run_id', 'workflow_run_revision', 'workflow_run_states', 'workflow_step_states', 'xinfo-input-result')
        diagnostic_nodes = 0
        diagnostic_overflow = False
        def diagnostic_value(value, depth=0):
            nonlocal diagnostic_nodes, diagnostic_overflow
            diagnostic_nodes += 1
            if depth > 8 or diagnostic_nodes > 65536:
                diagnostic_overflow = True
                return {"unavailable": "diagnostic_bound"}
            if value is None or type(value) in (bool, int):
                return {"type": type(value).__name__, "value": value}
            if type(value) is str:
                if value in diagnostic_source_tokens:
                    return {"type": "source_literal", "value": value}
                encoded = value.encode("utf-8")
                result = {"type": "str", "sha256": hashlib.sha256(encoded).hexdigest(),
                    "utf8_bytes": len(encoded), "codepoints": len(value),
                    "json_bytes": len(json.dumps(value, ensure_ascii=True).encode("ascii")),
                    "ascii": value.isascii()}
                if value in COMPOSITION_DESCRIPTORS:
                    result["source_table"] = value
                return result
            if type(value) in (tuple, list):
                if len(value) > 128:
                    diagnostic_overflow = True
                    return {"type": type(value).__name__, "count": len(value),
                        "unavailable": "diagnostic_bound"}
                return {"type": type(value).__name__, "count": len(value),
                    "items": [diagnostic_value(item, depth + 1) for item in value]}
            return {"unavailable": "non_numeric_value", "type": type(value).__name__}
        def error(exc):
            code = getattr(exc, "code", None)
            frames = []
            trace = exc.__traceback__
            while trace is not None:
                frame = trace.tb_frame
                entry = {"function": frame.f_code.co_name, "line": trace.tb_lineno,
                    "source": Path(frame.f_code.co_filename).name}
                # Exact original metadata frames only; no locals enumeration.
                if frame.f_code is observed_headers._metadata_rows.__code__:
                    entry["metadata_upper"] = diagnostic_value(frame.f_locals.get("upper"))
                    entry["metadata_appearance"] = diagnostic_value(frame.f_locals.get("appearance"))
                    entry["metadata_stage"] = frame.f_locals.get("stage")
                if frame.f_code is observed_headers._metadata_precharge.__code__:
                    entry["precharge_upper"] = diagnostic_value(frame.f_locals.get("upper"))
                    entry["precharge_appearance"] = diagnostic_value(frame.f_locals.get("appearance"))
                if frame.f_code is observed_headers._headers.__code__:
                    descriptor = frame.f_locals.get("descriptor")
                    if descriptor is COMPOSITION_DESCRIPTORS.get(getattr(descriptor, "table", None)):
                        entry["descriptor"] = descriptor.table
                        entry["key"] = diagnostic_value(frame.f_locals.get("key"))
                        entry["identity_count"] = len(frame.f_locals["identities"])
                        entry["row_bound"] = frame.f_locals.get("row_bound")
                        entry["source_name_factor"] = frame.f_locals.get("source_name_factor")
                        entry["column_lengths"] = [len(n) for n in descriptor.columns]
                        entry["kind_counts"] = {k: descriptor.kinds.count(k)
                            for k in ("text", "integer", "real", "blob")}
                        for numeric in ("key_copy_upper", "copy_upper", "fact_upper", "cost"):
                            value = frame.f_locals.get(numeric)
                            if type(value) is int:
                                entry[numeric] = value
                frames.append(entry)
                trace = trace.tb_next
            return {"class": type(exc).__name__, "code": code if type(code) is str
                and len(code) <= 128 and all(c in "abcdefghijklmnopqrstuvwxyz_" for c in code) else None,
                "frames": frames[-12:]}
        def observe_call(label, original):
            async def forwarded(*args, **kwargs):
                try:
                    if label == "auth":
                        import asyncio
                        from src.memory.header_bounds import HeaderReadBudget
                        original_debit = HeaderReadBudget.debit
                        original_task = asyncio.current_task()
                        first_budget = None
                        debit_count = 0
                        debit_depth = 0
                        charges = []
                        observations["auth_charge_trace"] = charges
                        certificate_ids = set()
                        observations["auth_completed_certificates"] = []
                        def observe_debit(budget, amount, *, appearance=None):
                            nonlocal first_budget, debit_count, debit_depth, diagnostic_overflow
                            entry = None
                            if asyncio.current_task() is original_task:
                                if first_budget is None:
                                    first_budget = budget
                                debit_count += 1
                                entry = {"count": debit_count, "depth": debit_depth,
                                    "same_first_budget": budget is first_budget,
                                    "amount": amount if type(amount) is int else None,
                                    "appearance": diagnostic_value(appearance),
                                    "remaining_before": budget.remaining}
                                # Source code identities only; no frame/owner is
                                # retained and no original proof helper is called.
                                frame = sys._getframe(1)
                                for _ in range(32):
                                    if frame is None:
                                        break
                                    if frame.f_code is observed_headers._certify_current_memory_snapshot_on_connection.__code__:
                                        entry["snapshot_frame_identity"] = id(frame)
                                        entry["snapshot_cookies"] = diagnostic_value(frame.f_locals.get("cookies"))
                                    if frame.f_code is observed_headers.preflight_composition_superset.__code__:
                                        state = frame.f_locals.get("state")
                                        if type(state) is tuple and len(state) == 3:
                                            entry["snapshot_transaction_identity"] = id(state[0])
                                            entry["snapshot_driver_identity"] = id(state[1])
                                            entry["snapshot_changes"] = state[2]
                                    if frame.f_code is observed_headers._metadata_rows.__code__:
                                        entry["stage"] = frame.f_locals.get("stage")
                                        entry["rows_upper"] = frame.f_locals.get("upper")
                                    if frame.f_code is observed_headers._headers.__code__:
                                        descriptor = frame.f_locals.get("descriptor")
                                        if descriptor is COMPOSITION_DESCRIPTORS.get(getattr(descriptor, "table", None)):
                                            entry["descriptor"] = descriptor.table
                                            entry["key"] = diagnostic_value(frame.f_locals.get("key"))
                                            entry["identity_count"] = len(frame.f_locals["identities"])
                                            entry["row_bound"] = frame.f_locals.get("row_bound")
                                            entry["source_name_factor"] = frame.f_locals.get("source_name_factor")
                                    frame = frame.f_back
                                del frame
                                certificate = observed_headers._CURRENT.get()
                                if (type(certificate) is observed_headers.CompositionHeaderCertificate
                                        and certificate.budget is budget
                                        and id(certificate) not in certificate_ids):
                                    if len(certificate_ids) < 32 and len(certificate.rows) <= 128:
                                        certificate_ids.add(id(certificate))
                                        observations["auth_completed_certificates"].append({
                                            "certificate_identity": id(certificate),
                                            "transaction_identity": id(certificate.transaction),
                                            "driver_identity": id(certificate.driver),
                                            "changes": certificate.changes,
                                            "schema_cookies": diagnostic_value(certificate.schema_cookies),
                                            "rows": [{"table": table,
                                                "key": diagnostic_value(key), "header_upper": upper}
                                                for (table, key), (_rowid, upper) in certificate.rows.items()]})
                                    else:
                                        diagnostic_overflow = True
                                observations["auth_debit"] = entry
                                if len(charges) < 4096:
                                    charges.append(entry)
                                else:
                                    diagnostic_overflow = True
                            debit_depth += 1
                            try:
                                return original_debit(budget, amount, appearance=appearance)
                            except BaseException as exc:
                                if entry is not None:
                                    entry["raised"] = type(exc).__name__
                                    entry["remaining_at_raise"] = budget.remaining
                                    observations.setdefault("auth_raising_charges", []).append(entry)
                                raise
                            finally:
                                debit_depth -= 1
                                if entry is not None:
                                    entry["remaining_after"] = budget.remaining
                        with monkeypatch.context() as auth_debit_patch:
                            auth_debit_patch.setattr(HeaderReadBudget, "debit", observe_debit)
                            result = await original(*args, **kwargs)
                    else:
                        result = await original(*args, **kwargs)
                except BaseException as exc:
                    observations[label] = error(exc)
                    raise
                observations[label] = {"returned": True}
                if label == "task":
                    observations[label].update(status=result.status.value,
                        revision=result.task_revision,
                        expected_revision=binding["board_task_revision"],
                        owner_matches=result.owner_principal_id == binding["owner_principal_id"],
                        session_matches=result.owner_session_id == binding["owner_session_id"],
                        artifact_matches=result.input_artifact_id == binding["input_artifact_id"])
                return result
            return forwarded
        def loaded_attempt(attempt, context):
            if attempt.attempt_id == binding["attempt_id"]:
                observations["attempt"] = {"loaded": True,
                    "task_matches": attempt.task_id == binding["task_id"],
                    "fence": attempt.fencing_token, "expected_fence": binding["board_fencing_token"],
                    "lease_owner_matches": attempt.lease_owner == dispatcher.runner_id,
                    "ended": attempt.ended_at is not None,
                    "cancel_requested": attempt.cancel_requested_at is not None,
                    "lease_present": attempt.lease_expires_at is not None}
        original_sessions = dispatcher.session_provider
        @asynccontextmanager
        async def observed_sessions():
            try:
                async with original_sessions() as db:
                    yield db
            except BaseException as exc:
                observations["session_exit"] = error(exc)
                raise
            observations["session_exit"] = {"returned": True}
        event.listen(WorkBoardAttempt, "load", loaded_attempt)
        try:
            with monkeypatch.context() as callback_patch:
                callback_patch.setattr(dispatcher, "session_provider", observed_sessions)
                callback_patch.setattr(board, "get_task", observe_call("task", board.get_task))
                callback_patch.setattr(dispatcher_module, "authenticate_session",
                    observe_call("auth", dispatcher_module.authenticate_session))
                callback_patch.setattr(opportunity_module, "stage_accepted_plan_task",
                    observe_call("accepted_plan", opportunity_module.stage_accepted_plan_task))
                callback_patch.setattr(pipelines, "task_guard", observe_call("pipeline", pipelines.task_guard))
                callback_patch.setattr(artifact_module, "resolve_input_artifact_for_task",
                    observe_call("artifact", artifact_module.resolve_input_artifact_for_task))
                result = await original_current(**binding)
                observations["original_returned_true"] = result is True
                return result
        finally:
            event.remove(WorkBoardAttempt, "load", loaded_attempt)
            if observations.get("original_returned_true") is not True:
                observations["diagnostic_complete"] = not diagnostic_overflow
                encoded_size = len(json.dumps(observations, sort_keys=True).encode("utf-8"))
                if encoded_size > 1048576:
                    observations = {"diagnostic_complete": False,
                        "diagnostic_overflow": True, "encoded_bytes": encoded_size}
                _save_private(root / "original-browser-admission-diagnostic.private.json", observations)
    monkeypatch.setattr(dispatcher, "_browser_assert_current", observe_current)
    async with async_playwright() as playwright:
        async def launch():
            # Supported original Playwright API: actual sandbox, no fallback.
            return await playwright.chromium.launch(headless=True, chromium_sandbox=True)
        with monkeypatch.context() as browser_patch:
            browser_patch.setattr(task_runner, "BrowserTaskRunner", lambda **kwargs: original_runner(
                **kwargs, browser_launcher=launch, transport_factory=lambda: PinnedBrowserTransport(
                    resolver=resolver, injected_fetch=fetch, site_policy=policy)))
            for index in range(3):
                task_id = operation["steps"][index]["task_id"]
                async with sessions() as db:
                    task = await board.get_task(db, owner, task_id)
                    await board.promote_task_ready(db, task_id, expected_revision=task.task_revision,
                        actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
                async with sessions() as db:
                    task = await board.get_task(db, owner, task_id)
                    claim = await board.claim_ready_task(db, task_id, expected_revision=task.task_revision,
                        lease_owner=dispatcher.runner_id, lease_seconds=180 if index == 0 else 30,
                        actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
                assert claim is not None
                outcome = await dispatcher._admit_execute_project(claim)
                assert outcome["completed"] is True
                async with sessions() as db:
                    task = await board.get_task(db, owner, task_id)
                    assert task.status == WorkBoardStatus.done
                    output = await pipelines.verified_output(db, owner, task)
                    assert read_output(output["file_path"], output["content_sha256"])
                    if index < 2:
                        operation = await pipelines.read(db, owner, operation["operation_id"])
                        operation = await pipelines.advance(db, owner, operation["operation_id"], operation["revision"])
                        await _commit_artifact_writer(db)
    assert fetches
    async with sessions() as db:
        attempt = (await db.scalars(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == task.task_id))).one()
        run = (await db.scalars(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == attempt.workflow_run_id))).one()
        assert attempt.outcome == "verified" and attempt.ended_at is not None
        assert attempt.lease_owner is None and attempt.lease_expires_at is None
        assert run.status == "succeeded" and run.finished_at is not None
        assert task.capability_id == "work.local-evidence-report.v1"
        output = await pipelines.verified_output(db, owner, task)
        assert read_output(output["file_path"], output["content_sha256"])
    return task, attempt, jobs


@pytest.mark.asyncio
async def test_genuine_terminal_report_observes_original_pre_effect_numeric_stop(
        accounting_db, real_auth, monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from src.db import engine as original
    from src.db.engine import get_session, override_session_factory
    from src.auth.service import create_session
    from src.auth.middleware import OperatorAuthMiddleware
    from src.db.models import Memory, MemoryProposal
    from src.memory import header_bounds, m5
    from src.memory.header_bounds import MAX_BYTES, MAX_ROWS, HeaderBoundsError
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.runtime_plugins.composition import reviewed_composition
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    from src.api.memory import router
    from config.settings import settings

    root, engine, factory = accounting_db
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    for target in ("src.db.engine.get_session", "src.auth.service.get_session", "src.goals.repository.get_session",
            "src.agent.session.get_session", "src.work_board.dispatcher.get_session", "src.workflows.job_runtime.get_session",
            "src.workflows.durable_state.get_session", "src.memory.repository.get_session", "src.audit.repository.get_session",
            "src.vault.repository.get_session", "src.approval.repository.get_session"):
        monkeypatch.setattr(target, get_session)
    receipt = {"schema_version": 1, "acceptance": False, "full_U": None,
        "terminal_report_verified": False, "factory_calls": 0, "tuple_available": False,
        "numeric_totals": None, "effect_calls": 0}
    host = None
    try:
        with override_session_factory(factory):
            await original.init_db()
            token, operator = await create_session()
            task, attempt, jobs = await _terminal_report(get_session, operator, monkeypatch, root)
            receipt["terminal_report_verified"] = True
            monkeypatch.setattr("src.workflows.job_runtime.durable_job_repository", jobs)
            node = Path(os.environ["SERAPH_CORDIS_TEST_NODE"])
            reviewed = reviewed_composition(node_path=node)
            workspace = ProductionWorkspace(host_root=root)
            with maintenance_fence(workspace):
                async with get_session() as db:
                    await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                    await initialize_fresh_deployment(db, composition_digests={
                        domain: reviewed.composition_digest for domain in DOMAINS})
            host = CordisHost(node_path=node, service_dispatch=NativeServiceDispatcher(jobs=jobs))
            monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
            assert await host.start()
            original_factory = header_bounds._reserve_native_memory_effect_ledger
            original_apply = m5._apply_native_memory_mutation_plan
            original_debit = header_bounds.HeaderReadBudget.debit
            async def count_memory():
                async with get_session() as db:
                    return (await db.scalar(select(func.count()).select_from(Memory)),
                        await db.scalar(select(func.count()).select_from(MemoryProposal)))
            before = await count_memory()
            async def apply(*args, **kwargs):
                receipt["effect_calls"] += 1
                return await original_apply(*args, **kwargs)
            async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
                receipt["factory_calls"] += 1
                assert receipt["factory_calls"] == 1 and MAX_BYTES == 1_048_576 and MAX_ROWS == 128
                guard = db.info["composition_guard"]
                budget = guard.header_budget
                assert budget is operation.source.admission.header_budget
                def debit(frame, amount, *, appearance=None):
                    try:
                        return original_debit(frame, amount, appearance=appearance)
                    except HeaderBoundsError:
                        if frame is budget:
                            caller = sys._getframe(1)
                            receipt["failed_original_debit"] = {"amount": amount,
                                "remaining": frame.remaining, "site_function": caller.f_code.co_name,
                                "site_line": caller.f_lineno}
                        raise
                try:
                    with monkeypatch.context() as observing:
                        observing.setattr(header_bounds.HeaderReadBudget, "debit", debit)
                        ledger = await original_factory(db, run, operation, prepared,
                            existing_references=existing_references, before_row=before_row, certificate=certificate)
                except HeaderBoundsError as error:
                    receipt["factory_error_class"] = type(error).__name__
                    receipt["factory_error_sha256"] = hashlib.sha256(str(error).encode()).hexdigest()
                    known_stops = {"memory_source_authority_numeric_contribution_unavailable",
                        "memory_complete_numeric_call_inventory_unavailable", "canonical_bound_not_certified",
                        "header_reference_bound"}
                    receipt["factory_stop_code"] = str(error) if str(error) in known_stops else None
                    receipt["source_spent_at_stop"] = MAX_BYTES - budget.remaining
                    totals = getattr(guard, "_native_memory_available_numeric_totals", None)
                    if totals is not None:
                        assert type(totals) is tuple and len(totals) == 8
                        assert totals[0] is operation and totals[1] is prepared and totals[2] is certificate
                        assert all(type(value) is int and value >= 0 for value in totals[3:])
                        receipt["tuple_available"] = True
                        receipt["original_identity_matches"] = True
                        receipt["numeric_totals"] = dict(zip(("source_spent", "effect_available",
                            "capture_available", "completion_available", "phase_count"), totals[3:]))
                    assert operation._effect is None and prepared.consumed is False
                    receipt["original_plan_unconsumed"] = True
                    assert receipt["factory_stop_code"] is not None
                    assert totals is not None or "failed_original_debit" in receipt
                    raise
                else:
                    ledger.close()
                    pytest.fail("original pre-effect STOP unexpectedly issued a ledger")
            monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
            monkeypatch.setattr(m5, "_apply_native_memory_mutation_plan", apply)
            app = FastAPI()
            app.add_middleware(OperatorAuthMiddleware)
            app.include_router(router, prefix="/api")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                    cookies={settings.operator_auth_cookie_name: token}, headers={"Origin": "http://localhost:3001"}) as client:
                response = await client.post("/api/memory/task-proposals/native", json={
                    "task_id": task.task_id, "expected_task_revision": task.task_revision,
                    "attempt_id": attempt.attempt_id, "idempotency_key": "genuine-report-numeric-prefix"})
                receipt["native_http_status"] = response.status_code
                assert response.status_code == 409
            assert receipt["factory_calls"] == 1 and receipt["effect_calls"] == 0
            assert await count_memory() == before
            receipt["memory_counts_unchanged"] = True
    except BaseException as error:
        receipt["test_error_class"] = type(error).__name__
        receipt["test_error_sha256"] = hashlib.sha256(str(error).encode()).hexdigest()
        raise
    finally:
        if host is not None:
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task
            receipt["native_host_reaped"] = host.snapshot()["cleanup"]["process_reaped"]
        _save_private(tmp_path / "genuine-report-numeric-prefix.json", receipt)
