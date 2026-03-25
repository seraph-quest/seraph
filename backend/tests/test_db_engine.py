from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from src.db.engine import _configure_sqlite_connection, _ensure_legacy_columns


async def test_ensure_legacy_columns_backfills_kind_from_category(tmp_path):
    db_path = tmp_path / "legacy-memory.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE memories (
                    id VARCHAR PRIMARY KEY,
                    content VARCHAR,
                    category VARCHAR,
                    source_session_id VARCHAR,
                    embedding_id VARCHAR,
                    created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO memories (id, content, category, source_session_id, embedding_id, created_at)
                VALUES ('goal-1', 'Ship batch A', 'goal', 's1', NULL, '2026-03-25T00:00:00Z')
                """
            )

            await _ensure_legacy_columns(conn)

            row = (
                await conn.exec_driver_sql(
                    "SELECT kind FROM memories WHERE id = 'goal-1'"
                )
            ).fetchone()
            assert row is not None
            assert row[0] == "goal"
    finally:
        await engine.dispose()


async def test_sqlite_connection_enables_foreign_keys(tmp_path):
    db_path = tmp_path / "fk-check.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.connect() as conn:
            row = (await conn.exec_driver_sql("PRAGMA foreign_keys")).fetchone()
            assert row is not None
            assert row[0] == 1
    finally:
        await engine.dispose()
