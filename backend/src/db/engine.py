import json
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Iterable

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.workspace import canonical_workspace_database_path, canonical_workspace_registry

_db_path = str(canonical_workspace_database_path(settings.workspace_dir))
_db_url = f"sqlite+aiosqlite:///{_db_path}"

engine = create_async_engine(
    _db_url,
    echo=settings.database_echo,
    connect_args={"check_same_thread": False},
    pool_size=20,
    max_overflow=0,
    pool_timeout=3,
    pool_pre_ping=True,
)


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

async_session_factory = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

OPERATOR_REQUIRED_TABLES = (
    "sessions",
    "messages",
    "approval_requests",
    "audit_events",
    "workflow_run_states",
    "workflow_step_states",
    "queued_insights",
    "native_notification_outbox",
    "native_notification_delivery_attempts",
    "telegram_transport_states",
    "telegram_inbound_updates",
    "telegram_transport_outbox",
    "telegram_delivery_attempts",
    "guardian_interventions",
    "strategy_deltas",
    "guardian_source_watches",
    "guardian_source_baselines",
    "guardian_decision_packets",
    "github_followthrough_connections",
    "guardian_routines",
    "guardian_routine_versions",
    "memory_tombstones",
    "audio_ingress_jobs",
    "audio_consent_grants",
    "work_board_tasks",
    "work_board_attempts",
    "work_board_review_intents",
    "work_board_links",
    "work_board_comments",
    "work_board_events",
    "work_board_proposals",
    "work_board_handoffs",
    "memory_proposals",
    "work_board_decision_receipts",
)

_LEGACY_WORKFLOW_STATUS_MAP = {
    "completed": "succeeded",
    "succeeded": "succeeded",
    "failed": "failed",
    "degraded": "degraded",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "queued": "queued",
    "awaiting_approval": "awaiting_approval",
    "paused": "paused",
    "blocked": "blocked",
    "accepted": "accepted",
    "pending": "accepted",
}

_SINGLE_OPERATOR_PRINCIPAL_ID = "operator:single"


def _map_legacy_workflow_status(status: str | None) -> tuple[str, str | None]:
    """Map a legacy workflow status into the bounded durable state machine."""
    original_status = str(status or "unknown")
    if original_status == "running":
        return "blocked", "migration_requires_reconciliation"
    if original_status not in _LEGACY_WORKFLOW_STATUS_MAP:
        return "blocked", "legacy_status_unmapped"
    return _LEGACY_WORKFLOW_STATUS_MAP[original_status], None


async def _ensure_legacy_columns(conn) -> None:
    """Backfill columns for older local SQLite databases."""
    async def _table_columns(table_name: str) -> set[str]:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
        return {row[1] for row in result.fetchall()}

    goal_columns = await _table_columns("goals")
    if goal_columns and "revision" not in goal_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE goals ADD COLUMN revision INTEGER DEFAULT 1"
        )
    if goal_columns and "revision" in await _table_columns("goals"):
        await conn.exec_driver_sql(
            "UPDATE goals SET revision = 1 WHERE revision IS NULL OR revision < 1"
        )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_goals_revision ON goals (revision)"
        )
    if goal_columns and "success_criterion_json" not in goal_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE goals ADD COLUMN success_criterion_json VARCHAR"
        )
    if goal_columns and "proactive_enabled" not in goal_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE goals ADD COLUMN proactive_enabled BOOLEAN DEFAULT 0"
        )
    if goal_columns and "proactive_enabled" in await _table_columns("goals"):
        await conn.exec_driver_sql(
            "UPDATE goals SET proactive_enabled = 0 WHERE proactive_enabled IS NULL"
        )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_goals_proactive_enabled ON goals (proactive_enabled)"
        )
    goal_columns = await _table_columns("goals")
    if goal_columns and "owner_principal_id" not in goal_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE goals ADD COLUMN owner_principal_id VARCHAR"
        )
    if goal_columns and "owner_session_id" not in goal_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE goals ADD COLUMN owner_session_id VARCHAR"
        )
    if goal_columns and "admission_budget_json" not in goal_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE goals ADD COLUMN admission_budget_json VARCHAR"
        )
    if goal_columns and "owner_principal_id" in await _table_columns("goals"):
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_goals_owner_principal_id ON goals (owner_principal_id)"
        )
    if goal_columns and "owner_session_id" in await _table_columns("goals"):
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_goals_owner_session_id ON goals (owner_session_id)"
        )

    user_profile_columns = await _table_columns("user_profiles")
    if user_profile_columns and "tool_policy_mode" not in user_profile_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE user_profiles ADD COLUMN tool_policy_mode VARCHAR DEFAULT 'full'"
        )
    if user_profile_columns and "mcp_policy_mode" not in user_profile_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE user_profiles ADD COLUMN mcp_policy_mode VARCHAR DEFAULT 'full'"
        )
    if user_profile_columns and "approval_mode" not in user_profile_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE user_profiles ADD COLUMN approval_mode VARCHAR DEFAULT 'high_risk'"
        )

    queued_insight_columns = await _table_columns("queued_insights")
    if queued_insight_columns and "intervention_id" not in queued_insight_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE queued_insights ADD COLUMN intervention_id VARCHAR"
        )
    if queued_insight_columns and "session_id" not in queued_insight_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE queued_insights ADD COLUMN session_id VARCHAR"
        )
    if queued_insight_columns and "owner_principal_id" not in queued_insight_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE queued_insights ADD COLUMN owner_principal_id VARCHAR"
        )
    if queued_insight_columns and "operator_session_id" not in queued_insight_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE queued_insights ADD COLUMN operator_session_id VARCHAR"
        )
    if queued_insight_columns and "goal_revision" not in queued_insight_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE queued_insights ADD COLUMN goal_revision INTEGER"
        )

    guardian_intervention_columns = await _table_columns("guardian_interventions")
    if guardian_intervention_columns and "active_project" not in guardian_intervention_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE guardian_interventions ADD COLUMN active_project VARCHAR"
        )
    if guardian_intervention_columns and "active_project" in await _table_columns(
        "guardian_interventions"
    ):
        await conn.exec_driver_sql(
            """
            CREATE INDEX IF NOT EXISTS ix_guardian_interventions_active_project
            ON guardian_interventions (active_project)
            """
        )

    memory_columns = await _table_columns("memories")
    if memory_columns and "kind" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN kind VARCHAR DEFAULT 'fact'"
        )
    if memory_columns and "kind" in await _table_columns("memories"):
        await conn.exec_driver_sql(
            """
            UPDATE memories
            SET kind = CASE category
                WHEN 'preference' THEN 'preference'
                WHEN 'pattern' THEN 'pattern'
                WHEN 'goal' THEN 'goal'
                WHEN 'reflection' THEN 'reflection'
                ELSE 'fact'
            END
            WHERE kind IS NULL
               OR kind = ''
               OR (kind = 'fact' AND category IN ('preference', 'pattern', 'goal', 'reflection'))
            """
        )
    if memory_columns and "summary" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN summary VARCHAR"
        )
    if memory_columns and "confidence" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN confidence FLOAT DEFAULT 0.5"
        )
    if memory_columns and "importance" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN importance FLOAT DEFAULT 0.5"
        )
    if memory_columns and "reinforcement" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN reinforcement FLOAT DEFAULT 1.0"
        )
    if memory_columns and "status" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN status VARCHAR DEFAULT 'active'"
        )
    if memory_columns and "subject_entity_id" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN subject_entity_id VARCHAR"
        )
    if memory_columns and "project_entity_id" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN project_entity_id VARCHAR"
        )
    if memory_columns and "metadata_json" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN metadata_json VARCHAR"
        )
    if memory_columns and "scope_key" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN scope_key VARCHAR"
        )
    if memory_columns and "updated_at" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN updated_at DATETIME"
        )
        await conn.exec_driver_sql(
            "UPDATE memories SET updated_at = created_at WHERE updated_at IS NULL"
        )
    if memory_columns and "last_confirmed_at" not in memory_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE memories ADD COLUMN last_confirmed_at DATETIME"
        )

    async def _add_missing_columns(
        table_name: str,
        definitions: dict[str, str],
    ) -> set[str]:
        columns = await _table_columns(table_name)
        if not columns:
            return columns
        for column, sql_type in definitions.items():
            if column not in columns:
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {column} {sql_type}"
                )
                columns.add(column)
        return columns

    await _add_missing_columns(
        "memory_snapshots",
        {"canonical_tombstone_revision": "VARCHAR"},
    )

    session_columns = await _add_missing_columns(
        "sessions",
        {"owner_principal_id": "VARCHAR"},
    )
    if "owner_principal_id" in session_columns:
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_sessions_owner_principal_id "
            "ON sessions (owner_principal_id)"
        )
        # Prior to the owner column, sessions represented cockpit conversations
        # and service-bound placeholder references alike.  Only rows with a
        # persisted user/assistant transcript are provably conversations, so
        # claim those legacy rows for the canonical single operator.  Empty
        # placeholders created by service/job references remain ownerless and
        # therefore fail closed until an authenticated chat ingress explicitly
        # binds them.
        message_columns = await _table_columns("messages")
        if (
            message_columns
            and {"session_id", "role"}.issubset(message_columns)
            and bool(settings.operator_auth_secret or settings.operator_auth_secret_hash)
        ):
            await conn.exec_driver_sql(
                "UPDATE sessions SET owner_principal_id = "
                ":owner_principal_id "
                "WHERE owner_principal_id IS NULL AND EXISTS ("
                "SELECT 1 FROM messages "
                "WHERE messages.session_id = sessions.id "
                "AND messages.role IN ('user', 'assistant')"
                ")",
                {"owner_principal_id": _SINGLE_OPERATOR_PRINCIPAL_ID},
            )

    # #750 adds queryable lineage to the existing transcript and durable
    # surfaces.  The session primary key remains the canonical conversation;
    # these additive fields are receipts, never an alternate identity store.
    message_lineage_columns = await _add_missing_columns(
        "messages",
        {
            "conversation_id": "VARCHAR",
            "thread_id": "VARCHAR",
            "owner_principal_id": "VARCHAR",
            "operator_session_id": "VARCHAR",
            "device_id": "VARCHAR",
            "channel": "VARCHAR",
            "transport": "VARCHAR",
            "correlation_id": "VARCHAR",
            "causation_id": "VARCHAR",
            "attachment_refs_json": "VARCHAR DEFAULT '[]'",
        },
    )
    for column in (
        "conversation_id",
        "thread_id",
        "owner_principal_id",
        "operator_session_id",
        "correlation_id",
    ):
        if column in message_lineage_columns:
            await conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_messages_{column} ON messages ({column})"
            )

    approval_lineage_columns = await _add_missing_columns(
        "approval_requests",
        {
            "conversation_id": "VARCHAR",
            "thread_id": "VARCHAR",
            "owner_principal_id": "VARCHAR",
            "operator_session_id": "VARCHAR",
            "device_id": "VARCHAR",
            "channel": "VARCHAR DEFAULT 'web'",
            "transport": "VARCHAR DEFAULT 'rest'",
            "correlation_id": "VARCHAR",
            "causation_id": "VARCHAR",
            "attachment_refs_json": "VARCHAR DEFAULT '[]'",
            "challenge": "VARCHAR",
            "action": "VARCHAR",
            "expires_at": "DATETIME",
        },
    )
    for column in (
        "conversation_id",
        "thread_id",
        "owner_principal_id",
        "operator_session_id",
        "correlation_id",
        "action",
        "expires_at",
    ):
        if column in approval_lineage_columns:
            await conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_approval_requests_{column} "
                f"ON approval_requests ({column})"
            )

    outbox_lineage_columns = await _add_missing_columns(
        "native_notification_outbox",
        {
            "goal_id": "VARCHAR",
            "goal_revision": "INTEGER",
            "budget_period_key": "VARCHAR",
            "budget_limit": "INTEGER",
            "operator_session_id": "VARCHAR",
            "device_id": "VARCHAR",
            "channel": "VARCHAR DEFAULT 'native_notification'",
            "transport": "VARCHAR DEFAULT 'native_notification'",
            "conversation_id": "VARCHAR",
            "correlation_id": "VARCHAR",
            "causation_id": "VARCHAR",
            "attachment_refs_json": "VARCHAR DEFAULT '[]'",
            "degraded_state": "VARCHAR",
        },
    )
    for column in (
        "goal_id",
        "goal_revision",
        "budget_period_key",
        "budget_limit",
        "operator_session_id",
        "device_id",
        "channel",
        "transport",
        "conversation_id",
        "correlation_id",
        "causation_id",
        "degraded_state",
    ):
        if column in outbox_lineage_columns:
            await conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_native_notification_outbox_{column} "
                f"ON native_notification_outbox ({column})"
            )

    insight_budget_columns = await _add_missing_columns(
        "queued_insights",
        {
            "goal_id": "VARCHAR",
            "goal_revision": "INTEGER",
            "budget_period_key": "VARCHAR",
            "budget_limit": "INTEGER",
        },
    )
    for column in ("goal_id", "goal_revision", "budget_period_key", "budget_limit"):
        if column in insight_budget_columns:
            await conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_queued_insights_{column} "
                f"ON queued_insights ({column})"
            )

    proof_columns = await _add_missing_columns(
        "model_capability_proofs",
        {
            "receipt_id": "VARCHAR DEFAULT 'legacy-unbound'",
            # A syntactically bounded sentinel keeps legacy rows ORM-readable but
            # cannot authorize them because it is not linked to a real receipt.
            "receipt_hash": (
                "VARCHAR DEFAULT "
                "'0000000000000000000000000000000000000000000000000000000000000000'"
            ),
        },
    )
    if "receipt_id" in proof_columns:
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_model_capability_proofs_receipt_id "
            "ON model_capability_proofs (receipt_id)"
        )

    # #751 keeps the requested capability durable so restart/process recovery
    # cannot silently reinterpret an audio request as chat.
    audio_ingress_columns = await _add_missing_columns(
        "audio_ingress_jobs",
        {
            "requested_capability": "VARCHAR DEFAULT 'chat'",
            "provider_status": "VARCHAR DEFAULT 'unverified'",
            "transport_status": "VARCHAR DEFAULT 'unknown'",
            "transport_lease_id": "VARCHAR",
            "cleanup_status": "VARCHAR DEFAULT 'complete'",
        },
    )
    if "requested_capability" in audio_ingress_columns:
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_audio_ingress_jobs_requested_capability "
            "ON audio_ingress_jobs (requested_capability)"
        )
    for column in ("provider_status", "transport_status", "transport_lease_id", "cleanup_status"):
        if column in audio_ingress_columns:
            await conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_audio_ingress_jobs_{column} "
                f"ON audio_ingress_jobs ({column})"
            )
    if "receipt_hash" in proof_columns:
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_model_capability_proofs_receipt_hash "
            "ON model_capability_proofs (receipt_hash)"
        )

    route_columns = await _add_missing_columns(
        "model_route_receipts",
        {
            "runtime_path": "VARCHAR DEFAULT 'legacy_unknown'",
            "fallback_used": "BOOLEAN DEFAULT 0",
            "fallback_reason_code": "VARCHAR",
            "degradation_codes_json": "VARCHAR DEFAULT '[]'",
            "cost_kind": "VARCHAR DEFAULT 'unknown'",
            "cost_amount": "FLOAT",
            "cost_currency": "VARCHAR",
            "cost_source": "VARCHAR",
            "cost_source_updated_at": "DATETIME",
            "usage_input_tokens": "INTEGER",
            "usage_output_tokens": "INTEGER",
            "usage_total_tokens": "INTEGER",
        },
    )
    if "runtime_path" in route_columns:
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_model_route_receipts_runtime_path "
            "ON model_route_receipts (runtime_path)"
        )

    await _add_missing_columns(
        "model_route_attempt_receipts",
        {
            "degradation_code": "VARCHAR",
            "usage_input_tokens": "INTEGER",
            "usage_output_tokens": "INTEGER",
            "usage_total_tokens": "INTEGER",
            "cost_kind": "VARCHAR DEFAULT 'unknown'",
            "cost_amount": "FLOAT",
            "cost_currency": "VARCHAR",
            "cost_source": "VARCHAR",
            "cost_source_updated_at": "DATETIME",
        },
    )

    # #743 adds the durable invocation contract to the existing workflow state
    # table.  The migration is additive: legacy workflow projections retain
    # their original status/payload and are handled by the legacy serializer;
    # only rows admitted through DurableJobRepository receive a binding and
    # participate in the typed lifecycle.
    workflow_job_columns = await _add_missing_columns(
        "workflow_run_states",
        {
            "record_schema_version": "INTEGER DEFAULT 1",
            "parent_job_id": "VARCHAR",
            "parent_fencing_token": "INTEGER",
            "job_kind": "VARCHAR DEFAULT 'workflow'",
            "owner_kind": "VARCHAR DEFAULT 'legacy'",
            "owner_principal_id": "VARCHAR",
            "service_id": "VARCHAR",
            # Keep execution conversation and browser operator authentication
            # as separate durable bindings for recovery authorization.
            "conversation_id": "VARCHAR",
            "operator_session_id": "VARCHAR",
            "goal_id": "VARCHAR",
            "goal_revision": "INTEGER",
            "plan_revision": "INTEGER",
            "candidate_id": "VARCHAR",
            "capability_version": "VARCHAR DEFAULT 'workflow-v1'",
            "input_digest": "VARCHAR",
            "authority_digest": "VARCHAR",
            "budget_digest": "VARCHAR",
            "idempotency_scope": "VARCHAR",
            "idempotency_key": "VARCHAR",
            "idempotency_binding": "VARCHAR",
            "priority": "INTEGER DEFAULT 50",
            "dependencies_json": "VARCHAR DEFAULT '[]'",
            "resource_claims_json": "VARCHAR DEFAULT '[]'",
            "declared_authority_json": "VARCHAR",
            "deadline_at": "DATETIME",
            "lease_owner": "VARCHAR",
            "lease_expires_at": "DATETIME",
            "fencing_token": "INTEGER DEFAULT 0",
            "revision": "INTEGER DEFAULT 0",
            "attempt_count": "INTEGER DEFAULT 0",
            "max_attempts": "INTEGER DEFAULT 1",
            "failure_reason": "VARCHAR",
            "checkpoint_receipts_json": "VARCHAR DEFAULT '[]'",
            "artifact_receipts_json": "VARCHAR DEFAULT '[]'",
            "effect_receipts_json": "VARCHAR DEFAULT '[]'",
            "result_digest": "VARCHAR",
            "result_summary": "VARCHAR",
        },
    )
    for column in ("conversation_id", "operator_session_id"):
        if column in workflow_job_columns:
            await conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_workflow_run_states_{column} "
                f"ON workflow_run_states ({column})"
            )
    if workflow_job_columns and "revision" in await _table_columns("workflow_run_states"):
        await conn.exec_driver_sql(
            "UPDATE workflow_run_states SET revision = 0 "
            "WHERE revision IS NULL OR revision < 0"
        )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_workflow_run_states_revision "
            "ON workflow_run_states (revision)"
        )
    # ``_add_missing_columns`` returns the existing set as well as newly added
    # columns, so this index is recreated on every startup even when a prior
    # migration already added the idempotency fields.
    if workflow_job_columns and "idempotency_binding" in workflow_job_columns:
        # Never let the unique index hide or discard a legacy dedupe conflict.
        # Ambiguous bindings need operator reconciliation while the table is
        # still readable; creating the index first would abort startup with a
        # backend-specific integrity error and obscure the affected rows.
        duplicate_bindings = await conn.exec_driver_sql(
            "SELECT idempotency_binding, COUNT(*) "
            "FROM workflow_run_states "
            "WHERE idempotency_binding IS NOT NULL "
            "GROUP BY idempotency_binding "
            "HAVING COUNT(*) > 1"
        )
        if duplicate_bindings.fetchall():
            duplicate_rows = await conn.exec_driver_sql(
                "SELECT id, status, run_fingerprint, metadata_json, idempotency_binding "
                "FROM workflow_run_states "
                "WHERE idempotency_binding IN ("
                "SELECT idempotency_binding FROM workflow_run_states "
                "WHERE idempotency_binding IS NOT NULL "
                "GROUP BY idempotency_binding HAVING COUNT(*) > 1"
                ")"
            )
            for row in duplicate_rows.fetchall():
                metadata = {}
                if row[3]:
                    try:
                        parsed = json.loads(row[3])
                        if isinstance(parsed, dict):
                            metadata = parsed
                    except (TypeError, json.JSONDecodeError):
                        metadata = {}
                metadata["durable_job_migration"] = {
                    "version": 1,
                    "original_status": str(row[1] or "unknown"),
                    "original_payload_digest": str(row[2] or ""),
                    "original_idempotency_binding": str(row[4] or ""),
                    "idempotency_conflict": True,
                    "preserved": True,
                    "operator_action": "reconcile_duplicate_idempotency_binding",
                }
                await conn.exec_driver_sql(
                    "UPDATE workflow_run_states SET status = 'blocked', "
                    "failure_reason = 'idempotency_binding_conflict', "
                    "record_schema_version = 2, idempotency_binding = NULL, "
                    "metadata_json = :metadata "
                    "WHERE id = :id",
                    {
                        "metadata": json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                        "id": row[0],
                    },
                )
            # Each conflict is now a visible blocked v2 row with its original
            # binding retained in migration metadata. Clearing only the
            # ambiguous index value lets the new unique index protect future
            # admissions without dropping or silently merging either row.
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ux_workflow_run_states_idempotency_binding "
            "ON workflow_run_states (idempotency_binding) "
            "WHERE idempotency_binding IS NOT NULL"
        )
        legacy_result = await conn.exec_driver_sql(
            "SELECT id, status, run_fingerprint, metadata_json "
            "FROM workflow_run_states "
            "WHERE record_schema_version = 1 AND idempotency_binding IS NULL"
        )
        legacy_rows = legacy_result.fetchall()
        for row in legacy_rows:
            metadata = {}
            if row[3]:
                try:
                    parsed = json.loads(row[3])
                    if isinstance(parsed, dict):
                        metadata = parsed
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
            migration_marker = metadata.get("durable_job_migration")
            if isinstance(migration_marker, dict) and migration_marker.get("version") == 1:
                continue
            original_status = str(row[1] or "unknown")
            migrated_status, failure_reason = _map_legacy_workflow_status(original_status)
            metadata["durable_job_migration"] = {
                "version": 1,
                "original_status": original_status,
                "original_payload_digest": str(row[2] or ""),
                "preserved": True,
            }
            await conn.exec_driver_sql(
                "UPDATE workflow_run_states SET status = :status, "
                "failure_reason = :failure_reason, metadata_json = :metadata "
                "WHERE id = :id",
                {
                    "status": migrated_status,
                    "failure_reason": failure_reason,
                    "metadata": json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                    "id": row[0],
                },
            )


async def _ensure_telegram_transport_columns(conn) -> None:
    """Add fields introduced after the initial #752 transport migration.

    ``SQLModel.metadata.create_all`` creates new tables but does not alter an
    existing SQLite table.  Keep this additive and idempotent so a workspace
    that already exercised the provider-free transport can restart safely
    after lease/readback hardening lands.
    """
    definitions = {
        "telegram_transport_states": {
            "last_update_at": "DATETIME",
            "last_error": "VARCHAR",
        },
        "telegram_transport_outbox": {
            "lease_owner": "VARCHAR",
            "lease_expires_at": "DATETIME",
            "fencing_token": "INTEGER DEFAULT 0",
            "deadline_at": "DATETIME",
            "cancelled_at": "DATETIME",
        },
        "telegram_delivery_attempts": {
            "lease_owner": "VARCHAR",
            "fencing_token": "INTEGER DEFAULT 0",
        },
    }
    for table_name, columns_to_add in definitions.items():
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
        existing = {row[1] for row in result.fetchall()}
        if not existing:
            continue
        for column, sql_type in columns_to_add.items():
            if column not in existing:
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {column} {sql_type}"
                )


async def _ensure_m5_indexes(conn) -> None:
    """Reassert the bounded M5 lookup indexes on an existing workspace."""

    # Recovery keeps the blocked/expired source projection and writes a new
    # proposal generation with the same preview digest.  Replace the original
    # pre-recovery index shape on existing SQLite workspaces before recreating
    # it with the terminal-row exclusion.
    await conn.exec_driver_sql(
        "DROP INDEX IF EXISTS ux_memory_proposals_owner_attempt_preview"
    )
    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_proposals_owner_attempt_preview "
        "ON memory_proposals (owner_principal_id, owner_session_id, source_task_id, source_attempt_id, preview_text_digest) "
        "WHERE preview_text_digest IS NOT NULL AND status NOT IN ('blocked', 'expired')",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_proposals_owner_attempt_no_learning "
        "ON memory_proposals (owner_principal_id, owner_session_id, source_task_id, source_attempt_id) "
        "WHERE status = 'no_learning'",
        "CREATE INDEX IF NOT EXISTS ix_memory_proposals_exact_comparison "
        "ON memory_proposals (owner_principal_id, owner_session_id, goal_id, goal_revision, "
        "capability_id, capability_version, typed_input_digest, source_context_digest, status, proposal_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_work_board_decision_receipts_binding "
        "ON work_board_decision_receipts (receipt_binding_digest)",
        "CREATE INDEX IF NOT EXISTS ix_work_board_decision_receipts_exact_intent "
        "ON work_board_decision_receipts (owner_principal_id, owner_session_id, later_task_id, "
        "later_task_revision, task_intent_digest, goal_id, goal_revision, capability_id, "
        "capability_version, typed_input_digest, source_context_digest, receipt_id)",
    )
    for statement in statements:
        await conn.exec_driver_sql(statement)


async def _ensure_m5_columns(conn) -> None:
    """Additive M5 columns for workspaces created by an earlier M5 build."""

    result = await conn.exec_driver_sql("PRAGMA table_info(memory_proposals)")
    existing = {row[1] for row in result.fetchall()}
    columns_to_add = {
        "candidate_set_digest": "VARCHAR DEFAULT ''",
        "acceptance_binding_digest": "VARCHAR",
        "artifact_ref": "VARCHAR",
        "artifact_digest": "VARCHAR",
        "rollback_reason": "VARCHAR DEFAULT ''",
        "recovered_from_proposal_id": "VARCHAR",
    }
    for column_name, sql_type in columns_to_add.items():
        if existing and column_name not in existing:
            await conn.exec_driver_sql(
                f"ALTER TABLE memory_proposals ADD COLUMN {column_name} {sql_type}"
            )

    result = await conn.exec_driver_sql("PRAGMA table_info(work_board_decision_receipts)")
    receipt_columns = {row[1] for row in result.fetchall()}
    if receipt_columns and "candidate_set_digest" not in receipt_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE work_board_decision_receipts ADD COLUMN candidate_set_digest VARCHAR DEFAULT ''"
        )
    if receipt_columns and "receipt_integrity_mac" not in receipt_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE work_board_decision_receipts ADD COLUMN receipt_integrity_mac VARCHAR"
        )


async def _ensure_search_indexes(conn) -> None:
    await conn.exec_driver_sql(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS session_recall_fts USING fts5(
            entry_key UNINDEXED,
            session_id UNINDEXED,
            entry_type UNINDEXED,
            source_label UNINDEXED,
            text,
            created_at UNINDEXED
        )
        """
    )

    trigger_statements = (
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_sessions_ai
        AFTER INSERT ON sessions
        BEGIN
            INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
            VALUES ('session:' || NEW.id, NEW.id, 'title', 'title', COALESCE(NEW.title, ''), COALESCE(NEW.updated_at, NEW.created_at));
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_sessions_au
        AFTER UPDATE OF title, updated_at ON sessions
        BEGIN
            DELETE FROM session_recall_fts WHERE entry_key = 'session:' || OLD.id;
            INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
            VALUES ('session:' || NEW.id, NEW.id, 'title', 'title', COALESCE(NEW.title, ''), COALESCE(NEW.updated_at, NEW.created_at));
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_sessions_ad
        AFTER DELETE ON sessions
        BEGIN
            DELETE FROM session_recall_fts WHERE entry_key = 'session:' || OLD.id;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_messages_ai
        AFTER INSERT ON messages
        WHEN NEW.role IN ('user', 'assistant')
        BEGIN
            INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
            VALUES ('message:' || NEW.id, NEW.session_id, 'message', NEW.role, COALESCE(NEW.content, ''), NEW.created_at);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_messages_au
        AFTER UPDATE OF role, content, session_id, created_at ON messages
        BEGIN
            DELETE FROM session_recall_fts WHERE entry_key = 'message:' || OLD.id;
            INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
            SELECT 'message:' || NEW.id, NEW.session_id, 'message', NEW.role, COALESCE(NEW.content, ''), NEW.created_at
            WHERE NEW.role IN ('user', 'assistant');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_messages_ad
        AFTER DELETE ON messages
        BEGIN
            DELETE FROM session_recall_fts WHERE entry_key = 'message:' || OLD.id;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_episodes_ai
        AFTER INSERT ON memory_episodes
        WHEN NEW.session_id IS NOT NULL AND NEW.episode_type != 'conversation'
        BEGIN
            INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
            VALUES (
                'episode:' || NEW.id,
                NEW.session_id,
                'event',
                COALESCE(NEW.episode_type, 'event'),
                TRIM(COALESCE(NEW.summary, '') || CHAR(10) || COALESCE(NEW.content, '')),
                COALESCE(NEW.observed_at, NEW.created_at)
            );
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_episodes_au
        AFTER UPDATE OF session_id, episode_type, summary, content, observed_at, created_at ON memory_episodes
        BEGIN
            DELETE FROM session_recall_fts WHERE entry_key = 'episode:' || OLD.id;
            INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
            SELECT
                'episode:' || NEW.id,
                NEW.session_id,
                'event',
                COALESCE(NEW.episode_type, 'event'),
                TRIM(COALESCE(NEW.summary, '') || CHAR(10) || COALESCE(NEW.content, '')),
                COALESCE(NEW.observed_at, NEW.created_at)
            WHERE NEW.session_id IS NOT NULL AND NEW.episode_type != 'conversation';
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS session_recall_episodes_ad
        AFTER DELETE ON memory_episodes
        BEGIN
            DELETE FROM session_recall_fts WHERE entry_key = 'episode:' || OLD.id;
        END
        """,
    )
    for statement in trigger_statements:
        await conn.exec_driver_sql(statement)

    await conn.exec_driver_sql("DELETE FROM session_recall_fts")
    await conn.exec_driver_sql(
        """
        INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
        SELECT
            'session:' || id,
            id,
            'title',
            'title',
            COALESCE(title, ''),
            COALESCE(updated_at, created_at)
        FROM sessions
        """
    )
    await conn.exec_driver_sql(
        """
        INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
        SELECT
            'message:' || id,
            session_id,
            'message',
            role,
            COALESCE(content, ''),
            created_at
        FROM messages
        WHERE role IN ('user', 'assistant')
        """
    )
    await conn.exec_driver_sql(
        """
        INSERT INTO session_recall_fts (entry_key, session_id, entry_type, source_label, text, created_at)
        SELECT
            'episode:' || id,
            session_id,
            'event',
            COALESCE(episode_type, 'event'),
            TRIM(COALESCE(summary, '') || CHAR(10) || COALESCE(content, '')),
            COALESCE(observed_at, created_at)
        FROM memory_episodes
        WHERE session_id IS NOT NULL
          AND episode_type != 'conversation'
        """
    )


async def _ensure_memory_indexes(conn) -> None:
    await conn.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_memories_kind_scope_key_unique
        ON memories (kind, scope_key)
        WHERE scope_key IS NOT NULL
        """
    )


async def _ensure_work_board_indexes(conn) -> None:
    """Add the M2 attempt uniqueness fences to existing workspaces."""

    # SQLModel metadata does not retrofit ``index=True`` columns added by an
    # ALTER TABLE on an existing workspace.  Review expiry is a bounded
    # scheduler sweep, so keep the upgraded schema indexed explicitly.
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_work_board_tasks_review_expires_at "
        "ON work_board_tasks (review_expires_at)"
    )
    await conn.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_work_board_attempts_active_task
        ON work_board_attempts (task_id)
        WHERE ended_at IS NULL
        """
    )
    # Handoff rows are immutable versions.  Older WIP databases used a
    # parent/child-only uniqueness key, which would discard a later verified
    # attempt.  Remove that obsolete index before installing the versioned
    # identity; the canonical row data remains intact.
    await conn.exec_driver_sql(
        "DROP INDEX IF EXISTS ux_work_board_handoffs_parent_child"
    )
    # Proposal idempotency is scoped to the exact parent revision. Older M4
    # workspaces may have either the revision-scoped index or the stricter
    # task-lifetime index. Upgrade both to the issue contract while preserving
    # historical rows. If malformed legacy data already has duplicate rows
    # for the exact scoped key, leave the rows intact and let the proposal
    # kernel return its typed reconciliation conflict.
    proposal_index = await conn.exec_driver_sql(
        "PRAGMA index_info(ux_work_board_proposals_idempotency)"
    )
    proposal_index_columns = [row[2] for row in proposal_index.fetchall()]
    expected_proposal_index_columns = [
        "owner_principal_id",
        "owner_session_id",
        "parent_task_id",
        "parent_revision",
        "kind",
        "idempotency_key",
    ]
    if proposal_index_columns != expected_proposal_index_columns:
        duplicate_result = await conn.exec_driver_sql(
            "SELECT owner_principal_id, owner_session_id, parent_task_id, parent_revision, kind, idempotency_key "
            "FROM work_board_proposals "
            "GROUP BY owner_principal_id, owner_session_id, parent_task_id, parent_revision, kind, idempotency_key "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
        if not duplicate_result.fetchone():
            await conn.exec_driver_sql("DROP INDEX IF EXISTS ux_work_board_proposals_idempotency")
            await conn.exec_driver_sql(
                """
                CREATE UNIQUE INDEX ux_work_board_proposals_idempotency
                ON work_board_proposals (
                    owner_principal_id,
                    owner_session_id,
                    parent_task_id,
                    parent_revision,
                    kind,
                    idempotency_key
                )
                """
            )
    # A prior WIP created this name without link_id.  SQLite's IF NOT EXISTS
    # would preserve that weaker shape, so recreate it to match the canonical
    # WorkBoardHandoff metadata exactly.
    await conn.exec_driver_sql(
        "DROP INDEX IF EXISTS ux_work_board_handoffs_version"
    )
    await conn.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_work_board_handoffs_version
        ON work_board_handoffs (
            owner_principal_id,
            owner_session_id,
            parent_task_id,
            child_task_id,
            link_id,
            source_attempt_id,
            source_task_revision
        )
        """
    )


async def _ensure_work_board_columns(conn) -> None:
    """Additive columns for existing canonical board workspaces."""

    result = await conn.exec_driver_sql("PRAGMA table_info(work_board_attempts)")
    columns = {row[1] for row in result.fetchall()}
    attempt_additions = {
        "cancel_requested_at": "DATETIME",
        "parent_handoff_context_json": "VARCHAR DEFAULT '[]'",
        "parent_handoff_digest": "VARCHAR",
    }
    for column, sql_type in attempt_additions.items():
        if columns and column not in columns:
            await conn.exec_driver_sql(
                f"ALTER TABLE work_board_attempts ADD COLUMN {column} {sql_type}"
            )
    task_result = await conn.exec_driver_sql("PRAGMA table_info(work_board_tasks)")
    task_columns = {row[1] for row in task_result.fetchall()}
    task_additions = {
        "review_expires_at": "DATETIME",
        "review_request_attempt_id": "VARCHAR",
        "review_request_fence": "INTEGER",
        "review_request_revision": "INTEGER",
        "review_request_digest": "VARCHAR",
        "review_request_evidence_json": "VARCHAR DEFAULT '[]'",
        "review_requested_at": "DATETIME",
    }
    for column, sql_type in task_additions.items():
        if task_columns and column not in task_columns:
            await conn.exec_driver_sql(
                f"ALTER TABLE work_board_tasks ADD COLUMN {column} {sql_type}"
            )
    if task_columns:
        # One-time legacy backfill: new review projections always persist an
        # expiry, so a NULL value identifies a row created before this
        # migration.  The predicate becomes false after this update and never
        # resets a later review window on startup.
        await conn.exec_driver_sql(
            "UPDATE work_board_tasks SET review_expires_at = "
            "datetime('now', '+7 days') "
            "WHERE status = 'review' AND review_expires_at IS NULL"
        )
    review_intent_result = await conn.exec_driver_sql(
        "PRAGMA table_info(work_board_review_intents)"
    )
    review_intent_columns = {row[1] for row in review_intent_result.fetchall()}
    if review_intent_columns and "workflow_run_id" not in review_intent_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE work_board_review_intents ADD COLUMN workflow_run_id VARCHAR DEFAULT ''"
        )
    if review_intent_columns:
        await conn.exec_driver_sql(
            "UPDATE work_board_review_intents SET workflow_run_id = ("
            "SELECT workflow_run_id FROM work_board_attempts a "
            "WHERE a.attempt_id = work_board_review_intents.attempt_id "
            "AND a.task_id = work_board_review_intents.task_id LIMIT 1) "
            "WHERE workflow_run_id IS NULL OR workflow_run_id = ''"
        )
    proposal_result = await conn.exec_driver_sql("PRAGMA table_info(work_board_proposals)")
    proposal_columns = {row[1] for row in proposal_result.fetchall()}
    proposal_additions = {
        "request_digest": "VARCHAR DEFAULT ''",
        "capability_id": "VARCHAR DEFAULT 'strategist_agent'",
        "capability_version": "VARCHAR DEFAULT ''",
        "authority_digest": "VARCHAR DEFAULT ''",
        "grant_revision": "INTEGER DEFAULT 1",
        "input_digest": "VARCHAR DEFAULT ''",
        "route_id": "VARCHAR DEFAULT 'strategist_agent'",
        "admission_job_id": "VARCHAR DEFAULT ''",
        "effect_id_digest": "VARCHAR DEFAULT ''",
        "provider_contact_started": "BOOLEAN DEFAULT 0",
        "provider_contact_state": "VARCHAR DEFAULT 'not_started'",
    }
    for column, sql_type in proposal_additions.items():
        if proposal_columns and column not in proposal_columns:
            await conn.exec_driver_sql(
                f"ALTER TABLE work_board_proposals ADD COLUMN {column} {sql_type}"
            )
    if proposal_columns:
        # Legacy proposal rows predate the durable operation binding.  Give
        # each one a stable private identity before SQLModel creates the
        # unique index; never reuse an empty value across rows.
        await conn.exec_driver_sql(
            "UPDATE work_board_proposals SET admission_job_id = "
            "'legacy:proposal:' || proposal_id "
            "WHERE admission_job_id IS NULL OR admission_job_id = ''"
        )
    handoff_result = await conn.exec_driver_sql("PRAGMA table_info(work_board_handoffs)")
    handoff_columns = {row[1] for row in handoff_result.fetchall()}
    handoff_additions = {
        "schema_version": "VARCHAR DEFAULT 'work_board_handoff.v1'",
        "link_id": "VARCHAR",
        # Older WIP rows only retained the workflow run, so add an explicit
        # empty sentinel first. The backfill binds only a unique exact
        # run/task match; unprovable history stays unverified.
        "source_attempt_id": "VARCHAR NOT NULL DEFAULT ''",
        "source_task_revision": "INTEGER DEFAULT 1",
        "risks_json": "VARCHAR DEFAULT '[]'",
    }
    added_source_attempt_id = bool(
        handoff_columns and "source_attempt_id" not in handoff_columns
    )
    for column, sql_type in handoff_additions.items():
        if handoff_columns and column not in handoff_columns:
            await conn.exec_driver_sql(
                f"ALTER TABLE work_board_handoffs ADD COLUMN {column} {sql_type}"
            )
    if added_source_attempt_id:
        attempt_result = await conn.exec_driver_sql(
            "PRAGMA table_info(work_board_attempts)"
        )
        attempt_columns = {row[1] for row in attempt_result.fetchall()}
        if {"attempt_id", "task_id", "workflow_run_id"}.issubset(attempt_columns):
            # A run ID identifies one source attempt only if it matches a
            # unique attempt for the parent. Ambiguous or missing history
            # retains the empty sentinel and fails normal proof validation.
            await conn.exec_driver_sql(
                "UPDATE work_board_handoffs SET source_attempt_id = ("
                "SELECT a.attempt_id FROM work_board_attempts a "
                "WHERE a.task_id = work_board_handoffs.parent_task_id "
                "AND a.workflow_run_id = work_board_handoffs.workflow_run_id "
                "LIMIT 1) WHERE source_attempt_id = '' AND ("
                "SELECT COUNT(*) FROM work_board_attempts a "
                "WHERE a.task_id = work_board_handoffs.parent_task_id "
                "AND a.workflow_run_id = work_board_handoffs.workflow_run_id"
                ") = 1"
            )
    if handoff_columns:
        await conn.exec_driver_sql(
            "UPDATE work_board_handoffs SET schema_version = 'work_board_handoff.v1' "
            "WHERE schema_version IS NULL OR schema_version = ''"
        )
    link_result = await conn.exec_driver_sql("PRAGMA table_info(work_board_links)")
    link_columns = {row[1] for row in link_result.fetchall()}
    if link_columns and "current_handoff_id" not in link_columns:
        await conn.exec_driver_sql(
            "ALTER TABLE work_board_links ADD COLUMN current_handoff_id VARCHAR"
        )
    if handoff_columns:
        # Existing handoffs were one-row-per-dependency.  Bind them to the
        # canonical link once; later attempts/revisions append new immutable
        # versions instead of overwriting this history.
        await conn.exec_driver_sql(
            "UPDATE work_board_handoffs SET link_id = ("
            "SELECT link_id FROM work_board_links l "
            "WHERE l.parent_task_id = work_board_handoffs.parent_task_id "
            "AND l.child_task_id = work_board_handoffs.child_task_id "
            "AND l.owner_principal_id = work_board_handoffs.owner_principal_id "
            "AND l.owner_session_id = work_board_handoffs.owner_session_id "
            "LIMIT 1) WHERE link_id IS NULL OR link_id = ''"
        )


async def init_db() -> None:
    """Create all tables on startup."""
    # Keep SQLite bound to the same canonical workspace registry used by
    # artifact and vault persistence.  New board tables are additive SQLModel
    # metadata and therefore participate in the canonical SQLite backup and
    # restore inventory without introducing a second migration framework.
    canonical_workspace_registry(settings.workspace_dir).classify_path("seraph.db")
    # Ensure every SQLModel table class is registered before create_all runs.
    from src.db import models as _models  # noqa: F401

    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    async with engine.begin() as conn:
        # Migrate an existing workflow table before SQLModel creates its
        # conditional unique idempotency index.  ``create_all`` attempts to
        # create model indexes for existing tables too; running it first would
        # make duplicate legacy bindings abort startup before the migration can
        # preserve and block those rows for operator reconciliation.
        await _ensure_legacy_columns(conn)
        await _ensure_telegram_transport_columns(conn)
        await _ensure_work_board_columns(conn)
        await conn.run_sync(SQLModel.metadata.create_all)
        await _ensure_m5_columns(conn)
        await _ensure_work_board_indexes(conn)
        await _ensure_m5_indexes(conn)
        await conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_work_board_attempts_workflow_run
            ON work_board_attempts (workflow_run_id)
            WHERE workflow_run_id IS NOT NULL
            """
        )
        await _ensure_memory_indexes(conn)
        await _ensure_search_indexes(conn)

    # Reconcile legacy Done-parent links once the additive board schema exists.
    # The helper only materializes proof-backed handoffs; rows without an
    # independently verified readback remain pointerless and therefore
    # ineligible for child dispatch.
    from src.work_board.review import backfill_verified_handoffs

    # Build this migration session from the current engine object.  Tests and
    # managed workspace lifecycle code may replace ``engine`` for an isolated
    # canonical workspace while leaving the module-level factory bound to the
    # default database.
    migration_factory = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with migration_factory() as migration_session:
        await backfill_verified_handoffs(migration_session)
        await migration_session.commit()


async def close_db() -> None:
    """Dispose of the engine on shutdown."""
    await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
            pending_work_board_events = session.info.pop(
                "work_board_events_after_commit", []
            )
            if pending_work_board_events:
                # The durable row is authoritative.  Queue a safe live update
                # only after commit so sockets never observe rolled-back state.
                from src.work_board.events import publish_work_board_events

                await publish_work_board_events(pending_work_board_events)
        except Exception:
            await session.rollback()
            session.info.pop("work_board_events_after_commit", None)
            raise


async def check_required_tables(required_tables: Iterable[str]) -> dict[str, object]:
    """Return a small operator-readable table readiness receipt."""
    required = tuple(
        dict.fromkeys(
            str(table).strip()
            for table in required_tables
            if str(table).strip()
        )
    )
    existing: set[str] = set()
    async with get_session() as db:
        for table in required:
            result = await db.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = :table_name"
                ),
                {"table_name": table},
            )
            if result.scalar_one_or_none():
                existing.add(table)
    missing = [table for table in required if table not in existing]
    return {
        "status": "ready" if not missing else "degraded",
        "required_tables": list(required),
        "missing_tables": missing,
    }
