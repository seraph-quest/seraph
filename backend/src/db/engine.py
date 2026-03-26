import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings

_db_path = os.path.join(settings.workspace_dir, "seraph.db")
_db_url = f"sqlite+aiosqlite:///{_db_path}"

engine = create_async_engine(
    _db_url,
    echo=settings.debug,
    connect_args={"check_same_thread": False},
)


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

async_session_factory = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_legacy_columns(conn) -> None:
    """Backfill columns for older local SQLite databases."""
    async def _table_columns(table_name: str) -> set[str]:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
        return {row[1] for row in result.fetchall()}

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
    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _ensure_legacy_columns(conn)
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
