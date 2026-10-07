"""Actual populated file-backed startup, additive upgrade and repeat startup."""
from datetime import datetime, timezone

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from config.settings import settings
from src.db import engine as db_engine


def test_canonical_managed_backup_restore_preserves_opportunity_rows_and_snapshot(monkeypatch, tmp_path):
    import json
    import sqlite3
    import os
    from uuid import uuid4
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlmodel import Session
    from cryptography.fernet import Fernet
    from src.db.models import Goal, GuardianOpportunity
    from tests.test_guardian_opportunity_contracts import evidence
    from src.guardian.opportunity_runtime import stage_snapshot, read_snapshot
    from src.workspace import (canonical_workspace_registry, WorkspaceStateClass, ProductionWorkspace,
        backup_workspace, restore_workspace, maintenance_fence)
    from src.workspace.production import reconcile_production_restore
    root = tmp_path/"workspace"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    registry = canonical_workspace_registry(root)
    # Populate the canonical managed workspace roots, including its recovery
    # key; production maintenance/backup validation remains unchanged.
    for spec in registry.config.declared_paths:
        path = root/spec.logical_path
        if spec.state_class == WorkspaceStateClass.CANONICAL and spec.logical_path != "seraph.db":
            if path.suffix:
                path.write_text("{}" if path.suffix == ".json" else "Local operator fixture\n")
            else:
                path.mkdir()
    (root/".vault-key").write_bytes(Fernet.generate_key())
    os.chmod(root/".vault-key", 0o600)
    offered = evidence()
    reference, sha = stage_snapshot(offered)
    engine = create_engine(f"sqlite:///{root/'seraph.db'}")
    SQLModel.metadata.create_all(engine)
    opportunity_id, current = str(uuid4()), datetime.now(timezone.utc)
    with Session(engine) as db:
        goal = Goal(id=str(uuid4()), title="Persisted operator Goal", revision=3,
            guardian_policy_revision=2, guardian_policy_json='{"preserved":true}')
        db.add(goal)
        db.add(GuardianOpportunity(id=opportunity_id, owner_principal_id="operator:root:persistence",
            original_root_id="persistence-root", goal_id=goal.id, goal_revision=3, policy_revision=2,
            watch_id=str(uuid4()), watch_revision=1, source_packet_id=str(offered.packet_id), source_digest=sha,
            source_token_json=json.dumps({"artifact_id": reference}), dedupe_key="a"*64, status="silent",
            expires_at=current+timedelta(hours=1), assessment_deadline_at=current+timedelta(seconds=120)))
        db.commit()
    engine.dispose()
    assert registry.classify_path(reference) == WorkspaceStateClass.CANONICAL
    manifest = registry.build_manifest()
    assert "guardian_opportunities" in json.dumps(manifest["database"])
    archive = tmp_path/"opportunity-backup.zip"
    with maintenance_fence(ProductionWorkspace(host_root=root)):
        backup_workspace(root, registry=registry, archive_path=archive)
        (root/reference).unlink()
        with sqlite3.connect(root/"seraph.db") as connection:
            connection.execute("DELETE FROM guardian_opportunities")
        restored = restore_workspace(root, archive, registry=registry, confirm=True,
            restore_id="restore-opportunity-01", reconcile_restore=reconcile_production_restore)
    assert restored["status"] == "restored"
    assert read_snapshot(reference, sha) == offered
    with sqlite3.connect(root/"seraph.db") as connection:
        restored_row = connection.execute("SELECT source_digest, status, policy_revision FROM guardian_opportunities WHERE id=?",
            (opportunity_id,)).fetchone()
        assert restored_row == (sha, "silent", 2)


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
            # This M4 index did not exist in the pre-M2 schema being reconstructed.
            await connection.exec_driver_sql("DROP INDEX ix_guardian_interventions_opportunity_feedback")
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
