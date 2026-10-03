"""Actual file-SQLite compatibility and durable mutation-key uniqueness.

This is schema recovery proof, not a fixture of executable task authority.
"""
import json

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.db.engine import OPERATOR_REQUIRED_TABLES, _ensure_work_board_columns
from src.db.models import SQLModel, WorkBoardEvidenceDependency


@pytest.mark.asyncio
async def test_legacy_events_upgrade_preserves_history_and_exact_mutation_keys(tmp_path):
    path = tmp_path / 'legacy-events.db'
    engine = create_async_engine('sqlite+aiosqlite:///' + str(path))
    old_metadata = json.dumps({'legacy': 'literal private text remains unchanged'})
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            'CREATE TABLE work_board_events ('
            'event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id VARCHAR NOT NULL,'
            'owner_principal_id VARCHAR NOT NULL, owner_session_id VARCHAR NOT NULL,'
            'actor_principal_id VARCHAR NOT NULL, actor_session_id VARCHAR,'
            'kind VARCHAR NOT NULL, metadata_json VARCHAR NOT NULL, created_at DATETIME NOT NULL)'
        )
        await conn.exec_driver_sql(
            "INSERT INTO work_board_events VALUES (1,'old-task','owner','session',"
            "'owner','session','task.created',?,'2026-10-03 10:00:00')", (old_metadata,)
        )
        await _ensure_work_board_columns(conn)
        await _ensure_work_board_columns(conn)
        before = (await conn.exec_driver_sql(
            'SELECT metadata_json,mutation_idempotency_key,mutation_request_digest '
            'FROM work_board_events WHERE event_id=1'
        )).one()
        assert tuple(before) == (old_metadata, None, None)
        # NULL ordinary events remain compatible and are not accidentally
        # collapsed into one mutation identity by the new unique index.
        for _ in range(2):
            await conn.exec_driver_sql(
                "INSERT INTO work_board_events (task_id,owner_principal_id,owner_session_id,"
                "actor_principal_id,kind,metadata_json,created_at) VALUES "
                "('old-task','owner','session','owner','task.note','{}','2026-10-03 10:00:01')"
            )
        key = 'a06bd829-3c76-49b5-a3ee-8789c6f9b938'
        insert = (
            'INSERT INTO work_board_events (task_id,owner_principal_id,owner_session_id,'
            'actor_principal_id,kind,metadata_json,created_at,mutation_idempotency_key,'
            "mutation_request_digest) VALUES (?,?,'session','owner',?,"
            "'{}','2026-10-03 10:00:02',?,?)"
        )
        await conn.exec_driver_sql(insert, ('old-task', 'owner', 'task.evidence.rebound', key, 'a'*64))
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                # Another task/kind/body cannot reuse the same owner/session
                # key and create a second successful receipt.
                await conn.exec_driver_sql(insert, ('other-task', 'owner', 'other.mutation', key, 'b'*64))
        await conn.exec_driver_sql(insert, ('other-task', 'other-owner', 'task.evidence.rebound', key, 'c'*64))
    await engine.dispose()
    reopened = create_async_engine('sqlite+aiosqlite:///' + str(path))
    try:
        async with reopened.connect() as conn:
            rows = (await conn.exec_driver_sql(
                'SELECT event_id,owner_principal_id,mutation_idempotency_key,'
                'mutation_request_digest FROM work_board_events ORDER BY event_id'
            )).all()
            assert len(rows) == 5
            assert tuple(rows[3]) == (4, 'owner', key, 'a'*64)
            assert tuple(rows[4]) == (5, 'other-owner', key, 'c'*64)
            assert (await conn.exec_driver_sql(
                'SELECT metadata_json FROM work_board_events WHERE event_id=1'
            )).scalar_one() == old_metadata
            indexes = (await conn.exec_driver_sql(
                'PRAGMA index_info(ux_work_board_events_mutation_key)'
            )).all()
            assert [row[2] for row in indexes] == [
                'owner_principal_id', 'owner_session_id', 'mutation_idempotency_key'
            ]
    finally:
        await reopened.dispose()
    print('ACTUAL_LEGACY_EVENT_DB=' + str(path))


def test_dependency_table_stays_in_existing_canonical_inventory():
    assert WorkBoardEvidenceDependency.__tablename__ in OPERATOR_REQUIRED_TABLES
    table = SQLModel.metadata.tables[WorkBoardEvidenceDependency.__tablename__]
    assert 'content' not in table.columns and 'text' not in table.columns
    assert any(index.name == 'ix_work_board_evidence_owner_source' for index in table.indexes)
