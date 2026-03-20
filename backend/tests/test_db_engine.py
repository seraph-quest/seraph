from sqlalchemy.ext.asyncio import create_async_engine

from src.db.engine import _ensure_legacy_columns


async def test_ensure_legacy_columns_adds_queued_insight_columns():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            """
            CREATE TABLE queued_insights (
                id VARCHAR PRIMARY KEY,
                content VARCHAR NOT NULL,
                intervention_type VARCHAR DEFAULT 'advisory',
                urgency INTEGER DEFAULT 3,
                reasoning VARCHAR DEFAULT '',
                created_at VARCHAR
            )
            """
        )
        await _ensure_legacy_columns(conn)
        result = await conn.exec_driver_sql("PRAGMA table_info(queued_insights)")
        columns = {row[1] for row in result.fetchall()}

    await engine.dispose()

    assert "intervention_id" in columns
    assert "session_id" in columns
