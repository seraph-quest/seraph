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
    max_overflow=20,
    pool_timeout=5,
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
    "guardian_interventions",
    "strategy_deltas",
    "memory_tombstones",
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


async def init_db() -> None:
    """Create all tables on startup."""
    # Keep SQLite bound to the same canonical workspace registry used by
    # artifact and vault persistence.  This is a path-ownership check only;
    # migration and backup/restore lifecycle work remains a later #742 slice.
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
        await conn.run_sync(SQLModel.metadata.create_all)
        await _ensure_memory_indexes(conn)
        await _ensure_search_indexes(conn)


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
        except Exception:
            await session.rollback()
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
