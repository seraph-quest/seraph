"""Capture provenance upgrade keeps populated ordinary Task rows unchanged."""
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from src.db import engine as db_engine


@pytest.mark.asyncio
async def test_capture_origin_additive_migration_preserves_old_rows_and_reruns(tmp_path):
    path = tmp_path / "capture-origin-upgrade.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE work_board_tasks (task_id TEXT PRIMARY KEY, status TEXT, idempotency_binding TEXT)")
        connection.execute("INSERT INTO work_board_tasks VALUES ('ordinary-original', 'triage', 'original-request-binding')")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        for _ in range(2):
            async with engine.begin() as connection:
                await db_engine._ensure_work_board_columns(connection)
    finally:
        await engine.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT task_id, idempotency_binding, channel_capture_origin_json FROM work_board_tasks").fetchall() == [
            ("ordinary-original", "original-request-binding", None)]
