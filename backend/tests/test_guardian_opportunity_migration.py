"""Actual populated file-backed startup, additive upgrade and repeat startup."""
from datetime import datetime, timezone

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from config.settings import settings
from src.db import engine as db_engine


async def test_populated_previous_database_survives_two_actual_startups(monkeypatch, tmp_path):
    database = tmp_path / "seraph.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    event.listen(engine.sync_engine, "connect", db_engine._configure_sqlite_connection)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(db_engine, "_db_path", str(database))
    monkeypatch.setattr(db_engine, "engine", engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
            await connection.exec_driver_sql("DROP TABLE guardian_opportunities")
            for table, columns in {
                "goals": ("guardian_policy_json", "guardian_policy_revision"),
                "guardian_decision_packets": ("opportunity_snapshot_artifact_id", "opportunity_snapshot_sha256"),
                "guardian_interventions": ("owner_principal_id", "original_root_id", "goal_id",
                    "goal_revision", "opportunity_id", "delivery_status"),
            }.items():
                for column in columns:
                    await connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
            # Real pre-existing Goal data remains authoritative, unopted-in.
            await connection.execute(text("INSERT INTO goals "
                "(id, title, path, level, status, domain, sort_order, revision, proactive_enabled, created_at, updated_at) "
                "VALUES ('legacy-goal', 'Existing goal', '/', 'project', 'active', 'productivity', 0, 7, 0, :now, :now)"),
                {"now": datetime.now(timezone.utc)})
        await db_engine.init_db()
        await db_engine.init_db()
        async with engine.connect() as connection:
            row = (await connection.exec_driver_sql("SELECT title, revision, guardian_policy_json, "
                "guardian_policy_revision FROM goals WHERE id='legacy-goal'")).one()
            assert tuple(row) == ("Existing goal", 7, None, 0)
            assert (await connection.exec_driver_sql("SELECT count(*) FROM guardian_opportunities")).scalar() == 0
            for index, columns in {
                "ux_guardian_opportunity_owner_dedupe": ["owner_principal_id", "dedupe_key"],
                "ix_guardian_opportunity_owner_goal_created": ["owner_principal_id", "goal_id", "created_at", "id"],
                "ix_guardian_opportunity_status_created": ["status", "created_at", "id"],
            }.items():
                rows = (await connection.exec_driver_sql(f"PRAGMA index_info({index})")).all()
                assert [item[2] for item in rows] == columns
    finally:
        await engine.dispose()
